#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import os
import time
import gevent
from django.http import StreamingHttpResponse, JsonResponse, HttpResponseRedirect
import pickle
import os, shutil, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


from apps.proxy.ts_proxy.stream_generator import StreamGenerator
from core.utils import RedisClient


TMS_MAXED_TTL_SEC = 10
TMS_MAXED_COUNTER = 2
TMS_MAXED_PKL = "/dev/shm/TMS/mark_maxed.pkl"
_tms_last_maxed = {}

def _TMS_mark_maxed(channel_id) -> None:
    channel_id = str(channel_id)
    print(f"TooManyStreams: Marking channel {channel_id} as maxed")
    # Set a short-lived flag that this channel recently hit maxed-out streams
    _exp_time = time.time() + TMS_MAXED_TTL_SEC
    if channel_id not in _tms_last_maxed:
        _tms_last_maxed[channel_id] = {"exp_time": _exp_time, "failed_counter": 1}
    else:
        _tms_last_maxed[channel_id].update({"exp_time": _exp_time, "failed_counter": _tms_last_maxed[channel_id]["failed_counter"] + 1})
    if not os.path.exists("/dev/shm/TMS"):
        try:
            os.makedirs("/dev/shm/TMS", exist_ok=True)
        except Exception:
            pass
    pickle.dump(_tms_last_maxed, open("/dev/shm/TMS/mark_maxed.pkl", "wb"))


    print(f"TooManyStreams: Marked channel {channel_id} as maxed until {_tms_last_maxed[channel_id]}")

def _TMS_is_maxed(channel_id) -> bool:
    channel_id = str(channel_id)
    # Check if this channel recently hit maxed-out streams
    if os.path.exists(TMS_MAXED_PKL):
        try:
            global _tms_last_maxed
            _tms_last_maxed = pickle.load(open(TMS_MAXED_PKL, "rb"))
            print(f"TooManyStreams: Loaded maxed info from {TMS_MAXED_PKL}: {_tms_last_maxed}")
        except Exception:
            pass

    if maxed := _tms_last_maxed.get(channel_id, None):
        print(f"TooManyStreams: Channel {channel_id} maxed info: {maxed}")
        _exp_time = maxed.get("exp_time", None)
        _failed_counter = maxed.get("failed_counter", None)

        if _exp_time is None or _failed_counter is None:
            return False

        if _failed_counter < TMS_MAXED_COUNTER:
            return False
        
        # if _exp_time < time.time():
        #     _tms_last_maxed.pop(channel_id, None)
        #     return False
        
        return True
    else:
        print(f"TooManyStreams: Channel {channel_id} has no maxed info")
    return False

CHUNK = 188 * 7  # 1316 is fine; larger also OK

def _ffmpeg_has(name: str) -> bool:
    try:
        from gevent import subprocess as gsubprocess; Subp = gsubprocess
    except Exception:
        import subprocess as gsubprocess; Subp = gsubprocess
    try:
        out = Subp.check_output(["ffmpeg","-hide_banner","-loglevel","error","-encoders"]).decode("utf-8","ignore")
    except Exception:
        return False
    return name in out

def still_ts_generator():
    import os, logging, time
    logger = logging.getLogger("plugins.too_many_streams")

    CHUNK = 188 * 7  # good TS chunk size
    img_path = os.path.join(os.path.dirname(__file__), "no_streams3.jpg")

    # gevent-friendly sleep & subprocess
    try:
        import gevent
        from gevent import subprocess as gsubprocess
        Subprocess = gsubprocess
        def _sleep(s): gevent.sleep(s)
    except Exception:
        import subprocess as gsubprocess
        Subprocess = gsubprocess
        def _sleep(s):
            import time
            time.sleep(s)

    def _ffmpeg_has(name: str) -> bool:
        try:
            out = Subprocess.check_output(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"]
            ).decode("utf-8", "ignore")
        except Exception:
            return False
        return name in out

    # Pick codecs (prefer h264/aac; fallback to mpeg2/mp2)
    h264 = _ffmpeg_has("libx264"); aac = _ffmpeg_has("aac")
    if h264 and aac:
        v_args = ["-c:v","libx264","-tune","stillimage","-pix_fmt","yuv420p",
                  "-profile:v","baseline","-level","3.0","-preset","veryfast",
                  "-r","25","-g","50","-keyint_min","50","-sc_threshold","0"]
        a_args = ["-c:a","aac","-b:a","96k","-ar","48000","-ac","2"]
        logger.info("TMS: using h264+aac for still stream")
    else:
        v_args = ["-c:v","mpeg2video","-q:v","2","-pix_fmt","yuv420p","-r","25","-g","50"]
        a_args = ["-c:a","mp2","-b:a","128k","-ar","48000","-ac","2"]
        logger.warning("TMS: falling back to mpeg2video+mp2 for still stream")

    base_cmd = [
        "ffmpeg",
        "-hide_banner","-loglevel","error","-nostdin",
        "-re",
        "-stream_loop","-1","-i", img_path,               # loop the still forever
        "-f","lavfi","-i","anullsrc=r=48000:cl=stereo",   # silent audio
        *v_args, *a_args,
        "-vf","scale=3840:-2,format=yuv420p,setsar=1",
        "-f","mpegts",
        "-muxrate","3500000","-maxrate","3500000","-minrate","3500000","-bufsize","7000000",
        "-pat_period","0.1",
        "-mpegts_flags","+initial_discontinuity",
        "-flush_packets","1",
        "pipe:1",
    ]

    null_ts = b"\x47" + b"\x1f\xff" + b"\x10" + b"\x00"*185

    def _stderr_pump(p):
        try:
            while True:
                line = p.stderr.readline()
                if not line:
                    break
                logger.error("TMS ffmpeg: %s", line.decode("utf-8","ignore").rstrip())
        except Exception:
            pass

    # Outer loop: restart ffmpeg if it ever exits (keeps stream looping forever)
    backoff = 0.25  # seconds, grows to avoid tight crash loops
    while True:
        if not os.path.exists(img_path):
            logger.error("TMS: image not found at %s; sending null TS keepalive", img_path)
            _sleep(0.5)
            yield null_ts
            continue

        try:
            proc = Subprocess.Popen(
                base_cmd, stdout=Subprocess.PIPE, stderr=Subprocess.PIPE, bufsize=0
            )
        except FileNotFoundError:
            logger.error("TMS: ffmpeg not found; sending null TS keepalive")
            _sleep(0.5)
            yield null_ts
            continue

        # Drain stderr (avoid deadlocks & get diagnostics)
        try:
            gevent.spawn(_stderr_pump, proc)  # if gevent present
        except Exception:
            import threading
            threading.Thread(target=_stderr_pump, args=(proc,), daemon=True).start()

        try:
            # Inner streaming loop
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    rc = proc.poll()
                    logger.warning("TMS: ffmpeg ended (rc=%s); restarting in %.2fs", rc, backoff)
                    _sleep(backoff)
                    backoff = min(backoff * 2, 5.0)  # cap backoff
                    break  # break inner loop -> restart
                else:
                    backoff = 0.25  # reset on healthy output
                    yield chunk
        except GeneratorExit:
            # client disconnected: stop ffmpeg and exit generator
            try:
                proc.terminate(); proc.wait(timeout=1)
            except Exception:
                try: proc.kill()
                except Exception: pass
            return
        except Exception:
            logger.exception("TMS: generator error; restarting in %.2fs", backoff)
            _sleep(backoff)
            backoff = min(backoff * 2, 5.0)
        finally:
            try:
                proc.terminate(); proc.wait(timeout=1)
            except Exception:
                try: proc.kill()
                except Exception: pass

logger = logging.getLogger('plugins.too_many_streams')
logger.setLevel(logging.DEBUG)
logger.info("TooManyStreams plugin initialized.")
class TooManyStreams:
    

    def __init__(self):
        # Create a logger for this plugin
        self.logger = logger
        
        self.last_maxed = {}  # channel_id -> expires_at(float)

        pass
    def check_streams(self):
        self.logger.info("Checking for too many streams.")
        # Implement the logic to check for too many streams
        pass


    def install_get_stream_override(self):
        # Import the class that owns get_stream (adjust import to your app)
        from apps.channels.models import Channel  # or whatever class defines get_stream
        
        if getattr(Channel, "_orig_get_stream", None) is None:
            self.logger.info("--------------------------Installing get_stream override in Channel class.")
            Channel._orig_get_stream = Channel.get_stream  # save original

            def _wrapped_get_stream(self, *args, **kwargs):
                """
                Finds an available stream for the requested channel and returns the selected stream and profile.

                Returns:
                    Tuple[Optional[int], Optional[int], Optional[str]]: (stream_id, profile_id, error_reason)
                """

                redis_client = RedisClient.get_client()
                error_reason = None

                # Check if this channel has any streams
                if not self.streams.exists():
                    error_reason = "No streams assigned to channel"
                    return None, None, error_reason

                # Check if a stream is already active for this channel
                stream_id_bytes = redis_client.get(f"channel_stream:{self.id}")
                if stream_id_bytes:
                    try:
                        stream_id = int(stream_id_bytes)
                        profile_id_bytes = redis_client.get(f"stream_profile:{stream_id}")
                        if profile_id_bytes:
                            try:
                                profile_id = int(profile_id_bytes)
                                return stream_id, profile_id, None
                            except (ValueError, TypeError):
                                logger.debug(
                                    f"Invalid profile ID retrieved from Redis: {profile_id_bytes}"
                                )
                    except (ValueError, TypeError):
                        logger.debug(
                            f"Invalid stream ID retrieved from Redis: {stream_id_bytes}"
                        )

                # No existing active stream, attempt to assign a new one
                has_streams_but_maxed_out = False
                has_active_profiles = False

                # Iterate through channel streams and their profiles
                for stream in self.streams.all().order_by("channelstream__order"):
                    # Retrieve the M3U account associated with the stream.
                    m3u_account = stream.m3u_account
                    if not m3u_account:
                        logger.debug(f"Stream {stream.id} has no M3U account")
                        continue

                    m3u_profiles = m3u_account.profiles.all()
                    default_profile = next(
                        (obj for obj in m3u_profiles if obj.is_default), None
                    )

                    if not default_profile:
                        logger.debug(f"M3U account {m3u_account.id} has no default profile")
                        continue

                    profiles = [default_profile] + [
                        obj for obj in m3u_profiles if not obj.is_default
                    ]

                    for profile in profiles:
                        # Skip inactive profiles
                        if not profile.is_active:
                            logger.debug(f"Skipping inactive profile {profile.id}")
                            continue

                        has_active_profiles = True

                        profile_connections_key = f"profile_connections:{profile.id}"
                        current_connections = int(
                            redis_client.get(profile_connections_key) or 0
                        )

                        # Check if profile has available slots (or unlimited connections)
                        if (
                            profile.max_streams == 0
                            or current_connections < profile.max_streams
                        ):
                            # Start a new stream
                            redis_client.set(f"channel_stream:{self.id}", stream.id)
                            redis_client.set(f"stream_profile:{stream.id}", profile.id)

                            # Increment connection count for profiles with limits
                            if profile.max_streams > 0:
                                redis_client.incr(profile_connections_key)

                            return (
                                stream.id,
                                profile.id,
                                None,
                            )  # Return newly assigned stream and matched profile
                        else:
                            # This profile is at max connections
                            has_streams_but_maxed_out = True
                            logger.debug(
                                f"Profile {profile.id} at max connections: {current_connections}/{profile.max_streams}"
                            )

                # No available streams - determine specific reason
                if has_streams_but_maxed_out:
                    #### TooManyStreams logic here ####
                    id_is_maxed = _TMS_is_maxed(self.id)
                    # uuid_is_maxed = _TMS_is_maxed(self.uuid)
                    logger.info(f"TMS: Channel {self.id} maxed={id_is_maxed}")
                    # logger.info(f"TMS: Channel {self.uuid} maxed={uuid_is_maxed}")
                                
                    if not id_is_maxed:
                        error_reason = "All M3U profiles have reached maximum connection limits" 
                        _TMS_mark_maxed(self.id)
                        _TMS_mark_maxed(self.uuid)
                        return None, None, error_reason
                    return stream.id, profile.id, None
                    #### TooManyStreams END logic here ####
                elif has_active_profiles:
                    error_reason = "No compatible profile found for any assigned stream"
                else:
                    error_reason = "No active profiles found for any assigned stream"

                return None, None, error_reason

            # Assigning a function to the class makes it a bound method automatically
            Channel.get_stream = _wrapped_get_stream
        else:
            self.logger.info("-------------------------get_stream override already installed, skipping.")




    def install_stream_ts_return_override(self):
        # 👉 Adjust this import to the module where stream_ts is defined
        # e.g. from apps.proxy.ts_proxy import views as ts_views
        from apps.proxy.ts_proxy import views as ts_views

        if getattr(ts_views, "_orig_stream_ts", None) is None:
            ts_views._orig_stream_ts = ts_views.stream_ts

            def _patched_stream_ts(request, channel_id, *args, **kwargs):
                resp = ts_views._orig_stream_ts(request, channel_id, *args, **kwargs)
                print("TMS: stream_ts called, got response type %s", type(resp))
                print("TMS: stream_ts called, got response status %s", getattr(resp, "status_code", "N/A")) 
                print("TMS: stream_ts called, got response status %s", type(getattr(resp, "status_code", "N/A"))) 
                print("TMS: stream_ts called, got response isinstance(resp, JsonResponse) %s", isinstance(resp, JsonResponse))
                print("TMS: stream_ts called, got channel_id %s", channel_id)
                stream_valid = _TMS_is_maxed(channel_id)
                print(f"TMS: stream_ts called, is_maxed={stream_valid} for channel {channel_id}")
                try:
                    if isinstance(resp, StreamingHttpResponse) and stream_valid and False:
                        # Build inputs for your generator
                        print("TMS: substituting StreamingHttpResponse for channel=%s (was 503 JSON).", channel_id)
                        client_ip = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                                    or request.META.get("REMOTE_ADDR"))
                        client_user_agent = request.META.get("HTTP_USER_AGENT")
                        client_id = getattr(request, "client_id", None) or f"tms-{int(time.time()*1000)}"
                        channel_initializing = True  # good default since we’re substituting init wait

                        generate = plugin_create_stream_generator(
                            channel_id, client_id, client_ip, client_user_agent, channel_initializing
                        )
                        response = StreamingHttpResponse(
                            streaming_content=generate(), content_type="video/mp2t"
                        )
                        response["Cache-Control"] = "no-cache"
                        print(f"TMS: substituting StreamingHttpResponse or channel {channel_id}")
                        print(f"TMS: substituting StreamingHttpResponse  for channel {channel_id}")
                        return response


                    else:
                        return 
                except Exception as e:
                    print(f"TMS: failed to substitute streaming response; using original 503 JSON. {e}")

                return resp

            ts_views.stream_ts = _patched_stream_ts
            print("TMS: installed stream_ts return override.")
        else:
            print("TMS: stream_ts override already installed; skipping.")


    def install_generate_patch(self):
        from apps.proxy.ts_proxy.stream_generator import StreamGenerator
        print("TMS------: Patching StreamGenerator.generate method.")

        if getattr(StreamGenerator, "_orig_generate", None) is None:
            StreamGenerator._orig_generate = StreamGenerator.generate
            print("TMS: Patching StreamGenerator.generate method.")
            def _patched_generate(self, *args, **kwargs):
                """
                Generator function that produces the stream content for the client.
                Handles initialization state, data delivery, and client disconnection.

                Yields:
                    bytes: Chunks of TS stream data
                """
                self.stream_start_time = time.time()
                self.bytes_sent = 0
                self.chunks_sent = 0

                try:
                    logger.info(f"[{self.client_id}] Stream generator started, channel_ready={not self.channel_initializing}")

                    # First handle initialization if needed
                    if self.channel_initializing:
                        channel_ready = self._wait_for_initialization()
                        if not channel_ready:
                            # If initialization failed or timed out, we've already sent error packets
                            return

                    # Channel is now ready - start normal streaming
                    logger.info(f"[{self.client_id}] Channel {self.channel_id} ready, starting normal streaming")

                    # Reset start time for real streaming
                    self.stream_start_time = time.time()

                    # Setup streaming parameters and verify resources
                    if not self._setup_streaming():
                        return

                    # Main streaming loop
                    for chunk in still_ts_generator() if _TMS_is_maxed(self.channel_id) else self._stream_data_generator():
                        yield chunk

                except Exception as e:
                    logger.error(f"[{self.client_id}] Stream error: {e}", exc_info=True)
                finally:
                    self._cleanup()
            
            StreamGenerator.generate = _patched_generate
                
            print("TMS: installed stream_ts return override.")

            # return StreamGenerator._orig_generate
        else:
            print("TMS: stream_ts override already installed; skipping.")

    @staticmethod
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
        print("TooManyStreams: Starting still image HTTP server on http://%s:%d/stream.ts", host, port)
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

    def TMS_generate_stream_url(self):
        from apps.proxy.ts_proxy import url_utils
        if getattr(url_utils, "_orig_generate_stream_url", None) is None:
            url_utils._orig_generate_stream_url = url_utils.generate_stream_url

            def _patched_generate_stream_url(channel_id:str):
                stream_url, stream_user_agent, transcode, profile_value = ( url_utils._orig_generate_stream_url(channel_id) )
                print(f"TMS: generate_stream_url called for channel {channel_id}, is_maxed={_TMS_is_maxed(channel_id)}")
                if _TMS_is_maxed(channel_id):
                    print(f"TMS: generate_stream_url substituting still image URL for channel {channel_id}")
                    stream_url = "http://127.0.0.1:8081/stream.ts"

                return stream_url, stream_user_agent, transcode, profile_value
            
            url_utils.generate_stream_url = _patched_generate_stream_url
        else:
            print("TMS: generate_stream_url override already installed; skipping.")
