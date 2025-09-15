#!/usr/bin/env python3
import os, shutil, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

def stream_still_mpegts_http(
    image_path: str,
    on_client_start: Optional[Callable[[], Optional[str]]] = None,
    host: str = "127.0.0.1",
    port: int = 8081,
) -> None:
    """
    Serve an infinite MPEG-TS stream over HTTP. For each client connection, an optional
    callback `on_client_start()` is invoked BEFORE ffmpeg starts. If the callback returns
    a string path, that image is used for this client; if it returns None, `image_path`
    is used.

    Open in VLC: Media -> Open Network Stream -> http://<host>:<port>/stream.ts
    (Default: http://127.0.0.1:8081/stream.ts)
    """

    def ffmpeg_or_die():
        exe = shutil.which("ffmpeg")
        if not exe:
            sys.exit("ERROR: ffmpeg not found in PATH. Install ffmpeg and try again.")
        return exe

    if not os.path.exists(image_path):
        sys.exit(f"ERROR: Image not found: {image_path}")

    ffmpeg_bin = ffmpeg_or_die()

    # Encoding defaults tuned for compatibility + quick startup for a still image
    fps = 25
    width = 1280
    v_bitrate = "3500k"
    a_bitrate = "128k"
    muxrate   = "3500k"
    bufsize   = "7000k"

    # Pre-detect encoders once (best-effort)
    def encoder_available(name: str) -> bool:
        try:
            out = subprocess.check_output([ffmpeg_bin, "-v", "0", "-hide_banner", "-encoders"], text=True)
            return f" {name} " in out
        except Exception:
            return False

    use_h264 = encoder_available("libx264")
    use_aac  = encoder_available("aac")

    # Build the command for a given image
    def make_ffmpeg_cmd(img: str):
        in_args = [
            "-loop", "1", "-framerate", str(fps), "-i", img,
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        ]
        map_args = ["-map", "0:v:0", "-map", "1:a:0"]
        v_args = [
            "-c:v", "libx264" if use_h264 else "mpeg2video",
            "-tune", "stillimage" if use_h264 else "film",
            "-pix_fmt", "yuv420p",
            "-profile:v", "baseline" if use_h264 else "main",
            "-level", "3.1" if use_h264 else "2.0",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2),
            "-r", str(fps),
            "-vf", f"scale={width}:-2,format=yuv420p,setsar=1",
            "-b:v", v_bitrate, "-maxrate", v_bitrate, "-minrate", v_bitrate,
            "-bufsize", bufsize,
        ]
        a_args = (["-c:a", "aac", "-b:a", a_bitrate] if use_aac else ["-c:a", "mp2", "-b:a", "192k"]) + [
            "-ar", "48000", "-ac", "2"
        ]
        ts_args = [
            "-muxrate", muxrate,
            "-pat_period", "0.1",
            "-mpegts_flags", "+initial_discontinuity",
            "-flush_packets", "1",
        ]
        # Output: write MPEG-TS to stdout
        out_args = ["-f", "mpegts", "pipe:1"]
        return [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                *in_args, *map_args, *v_args, *a_args, *ts_args, *out_args]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/stream.ts"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            # Pick image for THIS client
            chosen_img = None
            if on_client_start:
                try:
                    chosen_img = on_client_start()
                except Exception as e:
                    # Don't crash the server if the callback fails
                    print(f"[on_client_start] error: {e}", file=sys.stderr)
            if not chosen_img:
                chosen_img = image_path

            if not os.path.exists(chosen_img):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Image not found")
                return

            # Start ffmpeg for this client
            cmd = make_ffmpeg_cmd(chosen_img)
            print(f"[HTTP] Client {self.client_address} starting stream from: {chosen_img}")
            # Send headers first so VLC starts reading
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            try:
                with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
                    # Pump ffmpeg stdout to client until they disconnect
                    while True:
                        chunk = proc.stdout.read(1316)  # TS packet multiple
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            break
            finally:
                # Ensure the ffmpeg process is gone
                try:
                    if proc and proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                except Exception:
                    pass

        def log_message(self, fmt, *args):
            # Quieter server logs
            return

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"HTTP MPEG-TS server listening on http://{host}:{port}/stream.ts")
    print("Open in VLC: Media -> Open Network Stream -> http://127.0.0.1:8081/stream.ts")
    print("Press Ctrl+C to stop.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server…")
        # shutdown() from a non-main thread is safe; here we're in main.
        httpd.shutdown()
        httpd.server_close()

import itertools
images = itertools.cycle([
    r"C:\Users\james\Documents\GitHub\Dispatcharr_Too_Many_Streams\src\no_streams.jpg",
    r"C:\Users\james\Documents\GitHub\Dispatcharr_Too_Many_Streams\src\no_streams2.jpg",
    r"C:\Users\james\Documents\GitHub\Dispatcharr_Too_Many_Streams\src\no_streams3.jpg",
])

def on_join():
    # do logging, analytics, DB lookups, etc.
    print(">>> New client connected; rotating image…")
    return next(images)  # return the image to use for THIS client

stream_still_mpegts_http(r"C:\Users\james\Documents\GitHub\Dispatcharr_Too_Many_Streams\src\no_streams.jpg", on_client_start=on_join)