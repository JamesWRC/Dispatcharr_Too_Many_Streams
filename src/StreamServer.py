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

# Keep the encoder warm this long after the last client disconnects, so a quick
# re-tune / failover bounce doesn't pay the spin-up cost again.
IDLE_SHUTDOWN_GRACE = 60

# How often to re-check the active-stream list WHILE the placeholder is being
# shown. When nobody is connected, the updater blocks on the refresh signal and
# does zero Redis/DB polling.
ACTIVE_POLL_SECONDS = 60

# Minimum gap between content-driven encoder restarts. Every restart resets the
# MPEG-TS timestamps, which a downstream `-c copy` reader reports as a corrupt
# packet / backward DTS jump and may drop the stream over. On a busy box the
# active-channel set changes constantly, so without this the card can restart
# repeatedly and keep breaking its own viewers.
MIN_ENCODER_RESTART_INTERVAL = 30

# Frames per second for the card. This is a probe-reliability knob, not a
# picture-quality one -- the content is a still image, so any rate looks
# identical. At 1 fps a single keyframe was ~100KB and a reader had to wait a
# whole second per frame, so a client joining mid-stream frequently could not
# identify the video within its probe budget and hung until it gave up.
# ffmpeg's -analyzeduration counts STREAM time, which -re pins to wall-clock, so
# the only way to put more frames inside a reader's probe window is to raise the
# rate. 10 fps keeps each frame small and gives a joiner ~10 chances a second.
CARD_FPS = 10


class StreamServer:
    """
    Serves the 'Too Many Streams' placeholder as MPEG-TS over HTTP.

    A single shared FFmpeg encoder loops the still image into a CONTINUOUS,
    monotonically-timestamped MPEG-TS, and a broadcaster fans that out to every
    connected client. The encoder is started ON DEMAND (first client) and
    stopped after an idle grace period, so it costs nothing when no channel has
    failed over to the placeholder -- while still producing a clean continuous
    stream whenever it IS in use.

    NOTE: an earlier revision pre-encoded a short segment and looped the cached
    bytes to save CPU. That re-introduced the segment's timestamps on every loop,
    so the downstream proxy saw a backward DTS jump each cycle ("Packet corrupt
    ... corrupt input packet") and clients failed to play. A live encoder yields
    forever-increasing timestamps, which is why we keep one running while serving.
    """

    def __init__(self, host, port, image_path=None, refresh_signal=None):
        self.host = host
        self.port = port
        self.image_path = image_path or os.path.join(os.path.dirname(__file__), "..", "img", "too_many_streams2.jpg")
        self.refresh_signal = refresh_signal or threading.Event()

        self.process = None
        self.process_lock = threading.RLock()  # reentrant: _start_ffmpeg() calls _terminate_locked()
        self.clients = []
        self.clients_lock = threading.Lock()
        self._stop_timer = None
        self._render_lock = threading.Lock()
        self._last_start_ts = 0.0

        # Ensure image directory exists
        os.makedirs(os.path.dirname(self.image_path), exist_ok=True)

        self.ffmpeg_bin = shutil.which("ffmpeg")
        if not self.ffmpeg_bin:
            logger.error("FFmpeg not found! StreamServer cannot start.")

    # ------------------------------------------------------------------ #
    # FFmpeg command
    # ------------------------------------------------------------------ #
    def _get_ffmpeg_cmd(self, img_path):
        config = TooManyStreamsConfig.get_config()
        encoder = config.video_encoder or "libx264"

        cmd = [
            self.ffmpeg_bin,
            "-loglevel", "error",
            # -re paces each input to wall-clock so ffmpeg emits CARD_FPS in REAL
            # time. Without it, -framerate/-r only set timestamp rate and ffmpeg
            # encodes the looped image flat-out; the broadcaster drains (and
            # drops) frames with no backpressure, so a nominal low-fps encoder
            # pegged ~3 CPU cores the whole time a client was connected. -re only
            # paces reads -- it does NOT reintroduce the loop-wrap DTS jump.
            "-re", "-loop", "1",
            "-framerate", str(CARD_FPS),
            "-i", img_path,
            "-re", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-c:v", encoder,
        ]

        # Encoder-specific flags
        if "nvenc" in encoder:
            cmd.extend(["-preset", "p1", "-tune", "ull"])
        elif "qsv" in encoder:
            cmd.extend(["-preset", "veryfast"])
        else:
            # 1 fps with -g 1 (every frame an independent keyframe) gains nothing
            # from x264's frame threads; cap to a single thread so the encoder
            # can't briefly fan out across cores and to keep its footprint tiny.
            cmd.extend(["-preset", "ultrafast", "-tune", "stillimage", "-threads", "1"])

        cmd.extend([
            "-r", str(CARD_FPS),
            # One keyframe per second: a joiner never waits more than ~1s for an
            # entry point, without paying all-keyframes bitrate.
            "-g", str(CARD_FPS),
            "-b:v", "400k",
            "-c:a", "aac",
            "-b:a", "96k",
            "-f", "mpegts",
            "pipe:1",
        ])
        return cmd

    # ------------------------------------------------------------------ #
    # Encoder lifecycle (started on demand, stopped when idle)
    # ------------------------------------------------------------------ #
    def _terminate_locked(self):
        """Terminate the current encoder. Caller must hold process_lock."""
        if self.process:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()  # reap, else it lingers as a zombie
            except Exception as e:
                logger.warning(f"Error terminating FFmpeg: {e}")
            self.process = None

    def _start_ffmpeg(self):
        with self.process_lock:
            self._terminate_locked()

            if not os.path.exists(self.image_path):
                try:
                    PillowImageGen(out_path=self.image_path).generate(force=True)
                except Exception as e:
                    logger.error(f"Failed to generate initial image: {e}")

            cmd = self._get_ffmpeg_cmd(self.image_path)
            try:
                self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self._last_start_ts = time.time()
                logger.info("Placeholder encoder started.")
            except Exception as e:
                logger.error(f"Failed to start FFmpeg: {e}")
                self.process = None

    def _stop_ffmpeg(self):
        with self.process_lock:
            if self.process is not None:
                self._terminate_locked()
                logger.info("Placeholder encoder stopped (idle).")

    def _refresh_image_now(self):
        """Re-render the card synchronously (no-op if nothing changed)."""
        with self._render_lock:
            try:
                gen = PillowImageGen(out_path=self.image_path)
                gen.get_active_streams()
                gen.generate()
            except Exception as e:
                logger.error(f"Pre-start image refresh failed: {e}")

    def _ensure_running(self):
        """Start the encoder if it isn't already running, and cancel any pending idle-stop."""
        with self.process_lock:
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None
            needs_start = self.process is None or self.process.poll() is not None

        if needs_start:
            # Get the content right BEFORE spawning the encoder. Starting first
            # and letting the updater re-render a couple of seconds later meant
            # every fresh card view started an encoder, killed it, and started a
            # second one -- about 3s of the ~4s a viewer waited for first bytes.
            # Rendering first makes the updater's follow-up a no-op, because
            # generate() returns False when the signature is unchanged.
            # Deliberately done OUTSIDE process_lock: rendering can fetch channel
            # logos over HTTP, and the broadcaster takes that lock every pass.
            self._refresh_image_now()
            with self.process_lock:
                if self.process is None or self.process.poll() is not None:
                    self._start_ffmpeg()

    def _schedule_idle_stop(self):
        with self.process_lock:
            if self._stop_timer is not None:
                self._stop_timer.cancel()
            self._stop_timer = threading.Timer(IDLE_SHUTDOWN_GRACE, self._idle_stop_check)
            self._stop_timer.daemon = True
            self._stop_timer.start()

    def _idle_stop_check(self):
        with self.clients_lock:
            has_clients = len(self.clients) > 0
        if not has_clients:
            self._stop_ffmpeg()

    # ------------------------------------------------------------------ #
    # Broadcaster: read the shared encoder, fan out to client queues
    # ------------------------------------------------------------------ #
    def _broadcaster_loop(self):
        logger.info("Starting Broadcaster loop")
        while True:
            with self.process_lock:
                proc = self.process

            if not proc or not proc.stdout or proc.stdout.closed:
                time.sleep(0.2)
                continue

            with self.clients_lock:
                has_clients = len(self.clients) > 0
            if not has_clients:
                time.sleep(0.2)
                continue

            try:
                buf = proc.stdout.read(1316 * 16)  # 16 MPEG-TS packets
                if not buf:
                    # Encoder ended. Restart only if it's still the current
                    # process AND clients remain (otherwise let it stay down).
                    if proc.poll() is not None:
                        with self.process_lock:
                            still_current = self.process is proc
                        with self.clients_lock:
                            still_watched = len(self.clients) > 0
                        if still_current and still_watched:
                            logger.warning("Encoder exited unexpectedly; restarting.")
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
                time.sleep(0.5)

    # ------------------------------------------------------------------ #
    # Updater: re-render only on change, poll only while watched
    # ------------------------------------------------------------------ #
    def _image_updater_loop(self):
        logger.info("Starting Image Updater loop")
        # Render a correct resting image up front (active channels, or the
        # directory fallback) so the first frame a client sees is never the stale
        # empty "Unavailable" screen.
        try:
            gen = PillowImageGen(out_path=self.image_path)
            gen.get_active_streams()
            gen.generate(force=True)
        except Exception:
            pass

        while True:
            with self.clients_lock:
                has_clients = len(self.clients) > 0
            # While watched: poll periodically. While idle: block (zero polling).
            timeout = ACTIVE_POLL_SECONDS if has_clients else None
            signaled = self.refresh_signal.wait(timeout=timeout)
            self.refresh_signal.clear()

            if signaled:
                time.sleep(2)  # Buffer for DB consistency

            try:
                gen = PillowImageGen(out_path=self.image_path)
                if gen.get_active_streams() or signaled:
                    with self.process_lock:
                        running = self.process is not None and self.process.poll() is None
                    if running and (time.time() - self._last_start_ts) < MIN_ENCODER_RESTART_INTERVAL:
                        # Too soon to restart. Skip the render as well, so the
                        # change signature stays undetected and this same change
                        # is picked up again on the next pass -- rather than
                        # being consumed here and silently never shown.
                        logger.debug("Content changed but encoder restarted recently; deferring.")
                        continue
                    if gen.generate():
                        # Only restart the encoder if it's actually running (i.e.
                        # someone is watching). This is rare -- only when the set
                        # of active channels changes -- so the single resulting
                        # discontinuity is acceptable (vs every 30s when looping).
                        with self.process_lock:
                            running = self.process is not None and self.process.poll() is None
                        if running:
                            logger.info("Image changed; restarting encoder to show new content.")
                            self._start_ffmpeg()
            except Exception as e:
                logger.error(f"Image update failed: {e}")

    # ------------------------------------------------------------------ #
    # HTTP server
    # ------------------------------------------------------------------ #
    def start(self):
        if not self.ffmpeg_bin:
            return

        threading.Thread(target=self._image_updater_loop, daemon=True, name="TMS_ImageUpdater").start()
        threading.Thread(target=self._broadcaster_loop, daemon=True, name="TMS_Broadcaster").start()

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

                # Spin up the shared encoder (if not already) and trigger a refresh.
                server_instance._ensure_running()
                server_instance.refresh_signal.set()

                try:
                    while True:
                        try:
                            chunk = q.get(timeout=1.0)
                        except queue.Empty:
                            if getattr(self.wfile, "closed", False):
                                break
                            continue
                        self.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    pass
                except Exception:
                    pass
                finally:
                    with server_instance.clients_lock:
                        if q in server_instance.clients:
                            server_instance.clients.remove(q)
                        empty = len(server_instance.clients) == 0
                    if empty:
                        server_instance._schedule_idle_stop()

            def log_message(self, format, *args):
                pass

        logger.info(f"Starting TooManyStreams HTTP Server on {self.host}:{self.port}")
        # Allow reuse address to prevent "Address already in use" on quick restarts
        ThreadingHTTPServer.allow_reuse_address = True
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), StreamHTTPHandler)
        except OSError as e:
            # Another worker process already bound the port; don't crash the thread.
            logger.warning(f"TooManyStreams HTTP Server not started (port {self.port} unavailable): {e}")
            return
        try:
            httpd.serve_forever()
        except Exception as e:
            logger.error(f"HTTP Server crashed: {e}")
