"""Private, paced MPEG-TS warning server with owned resources and shutdown."""
import logging
import os
import queue
import select
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apps.proxy.live_proxy.utils import posix_spawn_proc
from django.db import close_old_connections

from .PillowImageGen import PillowImageGen
from .TooManyStreamsConfig import TooManyStreamsConfig

logger = logging.getLogger('plugins.too_many_streams.StreamServer')


class StreamServer:
    def __init__(self, host, port, image_path=None, refresh_signal=None):
        self.host, self.port = host, port
        self.static_image = bool(image_path)
        self.image_path = image_path or os.path.join(os.path.dirname(__file__), '..', 'img', 'too_many_streams.jpg')
        self.refresh_signal = refresh_signal or threading.Event()
        self.process = None
        self.clients = []
        self.clients_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._stopping = threading.Event()
        self._threads = []
        self._httpd = None
        self.ffmpeg_bin = shutil.which('ffmpeg')

    @property
    def is_running(self):
        proc = self.process
        return self._httpd is not None and not self._stopping.is_set() and proc is not None and proc.poll() is None

    def _get_ffmpeg_cmd(self, img_path):
        encoder = TooManyStreamsConfig.get_config().video_encoder or 'libx264'
        cmd = [self.ffmpeg_bin, '-hide_banner', '-loglevel', 'error', '-re',
               '-loop', '1', '-framerate', '1', '-i', img_path,
               '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-c:v', encoder]
        if 'nvenc' in encoder:
            cmd.extend(['-preset', 'p1', '-tune', 'ull'])
        elif 'qsv' in encoder:
            cmd.extend(['-preset', 'veryfast'])
        elif encoder == 'libx264':
            cmd.extend(['-preset', 'ultrafast', '-tune', 'stillimage'])
        cmd.extend(['-vf', 'scale=out_range=tv,format=yuv420p',
                    '-pix_fmt', 'yuv420p', '-color_range', 'tv',
                    '-r', '1', '-g', '1', '-b:v', '800k',
                    '-c:a', 'aac', '-b:a', '96k', '-f', 'mpegts', 'pipe:1'])
        return cmd

    @staticmethod
    def _close_process(proc):
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        finally:
            for name in ('stdin', 'stdout', 'stderr'):
                pipe = getattr(proc, name, None)
                if pipe is not None:
                    pipe.close()

    def _start_ffmpeg(self):
        # Never call this while holding process_lock. A single lock serializes
        # updater/recovery/stop and ensures that replaced children are reaped.
        with self.process_lock:
            proc, self.process = self.process, None
            self._close_process(proc)
            if self._stopping.is_set():
                return
            self.process = posix_spawn_proc(self._get_ffmpeg_cmd(self.image_path))
            self.process.stdin.close()  # no interactive control/input

    def _generate_image(self, force=False):
        try:
            gen = PillowImageGen(out_path=self.image_path, stop_event=self._stopping)
            changed = gen.get_active_streams()
            return gen.generate(force=force or changed)
        finally:
            close_old_connections()

    def _image_updater_loop(self):
        while not self._stopping.is_set():
            self.refresh_signal.wait(timeout=60)
            self.refresh_signal.clear()
            if self._stopping.is_set():
                break
            try:
                if self._generate_image():
                    self._start_ffmpeg()
            except Exception:
                logger.exception('Warning image update failed')

    def _broadcaster_loop(self):
        while not self._stopping.is_set():
            proc = self.process
            if proc is None or proc.poll() is not None:
                # Backoff applies to exited children too (e.g. invalid encoder).
                if self._stopping.wait(0.5):
                    break
                try:
                    self._start_ffmpeg()
                except Exception:
                    logger.exception('Could not restart warning FFmpeg')
                continue
            try:
                ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.2)
                for pipe in ready:
                    buf = os.read(pipe.fileno(), 1316 * 16)
                    if pipe is proc.stderr:
                        if buf:
                            logger.warning('Warning FFmpeg: %s', buf.decode(errors='replace').strip())
                        continue
                    if not buf:
                        continue
                    # Always drain stdout, including with no clients; an idle
                    # pipe must not stall the encoder or accumulate stale video.
                    with self.clients_lock:
                        for client in self.clients:
                            try:
                                client.put_nowait(buf)
                            except queue.Full:
                                # A slow consumer must not stall other viewers.
                                pass
            except (OSError, ValueError):
                # Updater/stop may have just closed this generation's descriptors.
                self._stopping.wait(0.05)

    def start(self):
        """Bind synchronously and return after starting owned background workers."""
        with self._lifecycle_lock:
            if self._httpd is not None:
                return
            if not self.ffmpeg_bin:
                raise RuntimeError('FFmpeg is required for the capacity warning')
            self._stopping.clear()
            instance = self

            class Handler(BaseHTTPRequestHandler):
                def setup(self):
                    super().setup()
                    # Bound request-line/header reads too, not just do_GET.
                    # Otherwise server_close can wait forever on an idle peer.
                    self.connection.settimeout(2)

                def do_GET(self):
                    self.connection.settimeout(2)
                    self.close_connection = True
                    if self.path not in ('/', '/stream.ts'):
                        self.send_error(404)
                        return
                    if not instance.is_running:
                        self.send_error(503)
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/mp2t')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    client = queue.Queue(maxsize=50)
                    with instance.clients_lock:
                        instance.clients.append(client)
                    instance.refresh_signal.set()
                    try:
                        while not instance._stopping.is_set():
                            try:
                                chunk = client.get(timeout=0.2)
                            except queue.Empty:
                                continue
                            self.wfile.write(chunk)
                    except (OSError, ValueError):
                        pass
                    finally:
                        with instance.clients_lock:
                            instance.clients.remove(client)

                def log_message(self, *_):
                    pass

            class Server(ThreadingHTTPServer):
                allow_reuse_address = True
                daemon_threads = False  # server_close joins bounded handlers

            httpd = Server((self.host, self.port), Handler)
            try:
                if self.static_image:
                    if not os.path.isfile(self.image_path):
                        raise RuntimeError('Static warning image does not exist')
                elif not self._generate_image(force=True):
                    raise RuntimeError('Could not generate initial warning image')
                self._start_ffmpeg()
                self._httpd = httpd
                self._threads = [
                    threading.Thread(target=self._broadcaster_loop, name='TMS_Broadcaster', daemon=True),
                    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.1), name='TMS_HTTP', daemon=True),
                ]
                if not self.static_image:
                    self._threads.append(threading.Thread(target=self._image_updater_loop, name='TMS_ImageUpdater', daemon=True))
                for thread in self._threads:
                    thread.start()
            except Exception:
                self._stopping.set()
                if self._httpd is not None:
                    self.stop()
                else:
                    httpd.server_close()
                    with self.process_lock:
                        proc, self.process = self.process, None
                        self._close_process(proc)
                raise

    def stop(self):
        with self._lifecycle_lock:
            self._stopping.set()
            self.refresh_signal.set()
            httpd, self._httpd = self._httpd, None
            if httpd is not None:
                if any(t.name == 'TMS_HTTP' and t.is_alive() for t in self._threads):
                    httpd.shutdown()
                httpd.server_close()
            with self.process_lock:
                proc, self.process = self.process, None
                self._close_process(proc)
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=5)
                    if thread.is_alive():
                        raise RuntimeError(f'Warning worker {thread.name} did not stop')
            self._threads.clear()
