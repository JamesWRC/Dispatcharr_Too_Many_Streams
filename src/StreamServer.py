import logging
import os
import shutil
import subprocess
import threading
import time
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .PillowImageGen import PillowImageGen
from .TooManyStreamsConfig import TooManyStreamsConfig

logger = logging.getLogger('plugins.too_many_streams.StreamServer')

class StreamServer:
    def __init__(self, host, port, image_path=None, refresh_signal=None):
        self.host = host
        self.port = port
        self.image_path = image_path or os.path.join(os.path.dirname(__file__), "..", "img", "too_many_streams2.jpg")
        self.refresh_signal = refresh_signal or threading.Event()
        
        self.process = None
        self.clients = []
        self.clients_lock = threading.Lock()
        self.process_lock = threading.Lock()
        
        # Ensure image directory exists
        os.makedirs(os.path.dirname(self.image_path), exist_ok=True)
        
        self.ffmpeg_bin = shutil.which("ffmpeg")
        if not self.ffmpeg_bin:
            logger.error("FFmpeg not found! StreamServer cannot start.")

    def _get_ffmpeg_cmd(self, img_path):
        config = TooManyStreamsConfig.get_config()
        encoder = config.video_encoder or "libx264"
        
        cmd = [
            self.ffmpeg_bin, 
            "-loop", "1", 
            "-framerate", "1", 
            "-i", img_path,
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-c:v", encoder,
        ]
        
        # Add encoder-specific flags
        if "nvenc" in encoder:
            cmd.extend(["-preset", "p1", "-tune", "ull"])
        elif "qsv" in encoder:
             cmd.extend(["-preset", "veryfast"])
        else:
             cmd.extend(["-preset", "ultrafast", "-tune", "stillimage"])

        cmd.extend([
            "-r", "1", 
            "-g", "1",
            "-b:v", "800k", 
            "-c:a", "aac", 
            "-b:a", "96k", 
            "-f", "mpegts", 
            "pipe:1"
        ])
        
        return cmd

    def _start_ffmpeg(self):
        with self.process_lock:
            if self.process:
                try:
                    if self.process.poll() is None:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                except Exception as e:
                    logger.warning(f"Error terminating FFmpeg: {e}")
            
            if not os.path.exists(self.image_path):
                 try:
                     PillowImageGen(out_path=self.image_path).generate(force=True)
                 except Exception as e:
                     logger.error(f"Failed to generate initial image: {e}")

            cmd = self._get_ffmpeg_cmd(self.image_path)
            # logger.debug(f"Starting FFmpeg: {' '.join(cmd)}")
            try:
                self.process = subprocess.Popen(
                    cmd, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.DEVNULL
                )
            except Exception as e:
                logger.error(f"Failed to start FFmpeg: {e}")
                self.process = None

    def _image_updater_loop(self):
        logger.info("Starting Image Updater loop")
        # Initial generation
        try:
            PillowImageGen(out_path=self.image_path).generate()
        except Exception: pass

        while True:
            # Wait for signal
            signaled = self.refresh_signal.wait(timeout=60)
            self.refresh_signal.clear()
            
            if signaled:
                time.sleep(2) # Buffer for DB consistency
            
            try:
                gen = PillowImageGen(out_path=self.image_path)
                # If content changed or we were explicitly signaled
                if gen.get_active_streams() or signaled:
                    if gen.generate():
                        logger.info("Image updated, restarting FFmpeg stream.")
                        self._start_ffmpeg()
            except Exception as e:
                logger.error(f"Image update failed: {e}")

    def _broadcaster_loop(self):
        logger.info("Starting Broadcaster loop")
        while True:
            # Safely get current process
            proc = self.process
            
            if not proc or not proc.stdout or proc.stdout.closed:
                time.sleep(0.5)
                # Check if we need to restart (e.g. startup failure)
                with self.process_lock:
                     if self.process is None:
                         self._start_ffmpeg()
                continue
            
            # Optimization: Pause if no clients
            with self.clients_lock:
                has_clients = len(self.clients) > 0

            if not has_clients:
                time.sleep(1)
                continue

            try:
                buf = proc.stdout.read(1316 * 16) # Read 16 MPEG-TS packets
                if not buf:
                    # Stream ended?
                    if proc.poll() is not None:
                        # Only restart if it's still the SAME process object (wasn't replaced by updater)
                        if self.process == proc:
                            logger.warning("FFmpeg process exited. Restarting.")
                            self._start_ffmpeg()
                    time.sleep(0.1)
                    continue
                
                with self.clients_lock:
                    for q in self.clients[:]:
                        try:
                            q.put_nowait(buf)
                        except queue.Full:
                            pass
            except Exception as e:
                logger.error(f"Broadcaster error: {e}")
                time.sleep(1)

    def start(self):
        if not self.ffmpeg_bin:
            return

        self._start_ffmpeg()

        threading.Thread(target=self._image_updater_loop, daemon=True, name="TMS_ImageUpdater").start()
        threading.Thread(target=self._broadcaster_loop, daemon=True, name="TMS_Broadcaster").start()

        # Capture 'self' for the handler
        server_instance = self

        class StreamHTTPHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/", "/stream.ts"):
                    self.send_response(404)
                    self.end_headers()
                    return

                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Connection", "keep-alive")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                q = queue.Queue(maxsize=50)
                with server_instance.clients_lock:
                    server_instance.clients.append(q)
                
                # Trigger a refresh
                server_instance.refresh_signal.set()

                try:
                    while True:
                        chunk = q.get()
                        self.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    pass
                except Exception as e:
                    # logger.debug(f"Client connection error: {e}")
                    pass
                finally:
                    with server_instance.clients_lock:
                        if q in server_instance.clients:
                            server_instance.clients.remove(q)

            def log_message(self, format, *args):
                pass

        logger.info(f"Starting TooManyStreams HTTP Server on {self.host}:{self.port}")
        # Allow reuse address to prevent "Address already in use" on quick restarts
        ThreadingHTTPServer.allow_reuse_address = True
        httpd = ThreadingHTTPServer((self.host, self.port), StreamHTTPHandler)
        try:
            httpd.serve_forever()
        except Exception as e:
            logger.error(f"HTTP Server crashed: {e}")
