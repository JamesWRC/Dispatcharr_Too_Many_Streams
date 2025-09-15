#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import os
import time
import gevent
from django.http import StreamingHttpResponse, JsonResponse, HttpResponseRedirect
import pickle

from apps.proxy.ts_proxy.stream_generator import StreamGenerator


TMS_MAXED_TTL_SEC = 10
TMS_MAXED_COUNTER = 2
TMS_MAXED_PKL = "/dev/shm/TMS/mark_maxed.pkl"
_tms_last_maxed = {}

def _TMS_mark_maxed(channel_id) -> None:
    channel_id = str(channel_id)

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

CHUNK = 1316  # 7 * 188, nice TS chunk size

def plugin_create_stream_generator(channel_id, client_id, client_ip, client_user_agent, channel_initializing):
    img_path = os.path.join(os.path.dirname(__file__), "no_streams.jpg")
    print("TooManyStreams: --------------------------views.create_stream_generator called")
    print("TooManyStreams: --------------------------channel_id: %s", channel_id)
    print("TooManyStreams: --------------------------client_id: %s", client_id)
    print("TooManyStreams: --------------------------client_ip: %s", client_ip)
    print("TooManyStreams: --------------------------client_user_agent: %s", client_user_agent)
    print("TooManyStreams: --------------------------channel_initializing: %s", channel_initializing)
    if _TMS_is_maxed(channel_id):
        generator = StreamGenerator(channel_id, client_id, client_ip, client_user_agent, channel_initializing)
        print("TooManyStreams: --------------------------Returning custom generator for channel %s", channel_id)
        return generator

    # gevent-friendly subprocess if present
    try:
        from gevent import subprocess as gsubprocess
        Subprocess = gsubprocess
    except Exception:
        import subprocess as gsubprocess
        Subprocess = gsubprocess

    # Prefer h264/aac, fall back to mpeg2/mp2 if not available
    def _encoders():
        try:
            out = Subprocess.check_output(["ffmpeg","-hide_banner","-loglevel","error","-encoders"]).decode("utf-8","ignore")
        except Exception:
            return ("mpeg2video","mp2")
        v = "libx264" if "libx264" in out else "mpeg2video"
        a = "aac"     if "aac"     in out else "mp2"
        return (v, a)

    venc, aenc = _encoders()
    if venc == "libx264":
        v_args = ["-c:v","libx264","-tune","stillimage","-pix_fmt","yuv420p","-profile:v","baseline","-level","3.0","-preset","veryfast","-r","25","-g","50"]
    else:
        v_args = ["-c:v","mpeg2video","-q:v","2","-pix_fmt","yuv420p","-r","25","-g","50"]

    if aenc == "aac":
        a_args = ["-c:a","aac","-b:a","96k","-ar","48000","-ac","2"]
    else:
        a_args = ["-c:a","mp2","-b:a","128k","-ar","48000","-ac","2"]

    # cmd = [
    #     "ffmpeg",
    #     "-hide_banner","-loglevel","error","-nostdin",
    #     "-re",
    #     "-stream_loop","-1", "-i", img_path,                 # loop the still forever
    #     "-f","lavfi","-i","anullsrc=r=48000:cl=stereo",      # silent audio
    #     *v_args, *a_args,
    #     "-vf","scale=1280:-2,format=yuv420p,setsar=1",       # friendly for most players
    #     "-f","mpegts",
    #     "-mpegts_flags","+initial_discontinuity",
    #     "-flush_packets","1",
    #     "pipe:1",
    # ]

    cmd = [
        # ffmpeg -hide_banner -loglevel error -i no_streams.jpg -f rawvideo -pix_fmt rgb24 -
        'ffmpeg', "-hide_banner","-loglevel","error", '-i', img_path, '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']

    def generate():
        if not os.path.exists(img_path):
            null_ts = b"\x47" + b"\x1f\xff" + b"\x10" + b"\x00"*185
            while True: yield null_ts

        try:
            proc = Subprocess.Popen(cmd, stdout=Subprocess.PIPE, stderr=Subprocess.PIPE, bufsize=0)
        except FileNotFoundError:
            print("TMS: ffmpeg not found; falling back to null TS")
            null_ts = b"\x47" + b"\x1f\xff" + b"\x10" + b"\x00"*185
            while True: yield null_ts

        # pump stderr for debug (non-blocking)
        def _stderr_pump(p):
            try:
                while True:
                    line = p.stderr.readline()
                    print(f"TMS ffmpeg: stderr read line: {line}")
                    if not line: break
                    print(f"TMS ffmpeg: { line.decode('utf-8','ignore').rstrip()}")
            except Exception as e: 
                print(f"TMS: ffmpeg stderr pump error {e}")

        try:
            try:
                import gevent; gevent.spawn(_stderr_pump, proc)
            except Exception:
                import threading; threading.Thread(target=_stderr_pump, args=(proc,), daemon=True).start()

            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    rc = proc.poll()
                    print(f"TMS: ffmpeg exited rc={rc}; stopping")
                    break
                print(f"TMS: yielding {chunk} bytes")
                print(f"TMS: yielding {len(chunk)} bytes")
                yield chunk
        except GeneratorExit:
            pass
        except Exception:
            print("TMS: generator error")
        finally:
            try:
                proc.terminate(); proc.wait(timeout=1)
            except Exception:
                try: proc.kill()
                except Exception: pass

    return generate

class TooManyStreams:
    

    def __init__(self):
        # Create a logger for this plugin
        self.logger = logging.getLogger('plugins.too_many_streams')
        self.logger.setLevel(logging.DEBUG)
        self.logger.info("TooManyStreams plugin initialized.")
        
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
                stream_id, profile_id, error_reason = Channel._orig_get_stream(self, *args, **kwargs)

                # Only override when "has_streams_but_maxed_out" would have been True
                if error_reason == "All M3U profiles have reached maximum connection limits":
                    # 👉 Put your custom behavior here:
                    # - change the message
                    # - trigger your UI flow
                    # - fall back to a special stream/profile
                    # Example: return a custom message
                    error_reason = "Too many streams (custom policy): please try again shortly."

                    # Or, if you want to return a sentinel profile/stream:
                    # stream_id, profile_id = None, None
                    # error_reason = "too_many_streams_custom"

                return stream_id, profile_id, error_reason

            # Assigning a function to the class makes it a bound method automatically
            Channel.get_stream = _wrapped_get_stream
        else:
            self.logger.info("-------------------------get_stream override already installed, skipping.")




    # --- YOUR custom generator factory ---
   


    # def plugin_create_stream_generator(channel_id, client_id, client_ip, client_user_agent, channel_initializing):
    #     """
    #     Return a generator *factory* that emits your custom 'too many streams' content.
    #     Must match the signature of the original create_stream_generator.
    #     """

    #     def generate():
    #         # Example: keep connection alive with short null TS packets while your UI shows a message
    #         # Replace with your real content/TS stream (e.g., pre-encoded "too many streams" clip).
    #         null_ts_packet = b"\x47" + b"\x1f\xff" + b"\x10" + b"\x00" * 185  # PID 0x1FFF null
    #         while True:
    #             print("TooManyStreams: yielding null TS packet to keep connection alive.")
    #             yield null_ts_packet
    #             gevent.sleep(0.5)

    #     return generate

    def install_overrides(self):
        # 1) Patch the method that *discovers* the maxed-out condition
        from apps.channels.models import Channel  # <-- adjust if your class lives elsewhere

        if getattr(Channel, "_orig_get_stream", None) is None:
            self.logger.info("--------------------------Installing get_stream patch in Channel class.")
            Channel._orig_get_stream = Channel.get_stream

            def _patched_get_stream(self, *args, **kwargs):
                print("TooManyStreams: --------------------------Channel.get_stream called")
                print("TooManyStreams: --------------------------args: %s", args)
                print("TooManyStreams: --------------------------kwargs: %s", kwargs)
                stream_id, profile_id, error_reason = Channel._orig_get_stream(self, *args, **kwargs)
                # If the call landed in the "max connection limits" branch, flag it for a short window
                if error_reason and "maximum connection limits" in error_reason.lower():
                    print(f"TooManyStreams: Channel {self.id} hit maxed-out streams.")
                    print(f"TooManyStreams: Channel {self.uuid} hit maxed-out streams.")
                    _TMS_mark_maxed(self.id)
                    _TMS_mark_maxed(self.uuid)
                return stream_id, profile_id, error_reason

            Channel.get_stream = _patched_get_stream
            self.logger.info("TooManyStreams: --------------------------Patched Channel.get_stream to detect 'maximum connection limits'")
        else:
            self.logger.info("TooManyStreams: --------------------------Channel.get_stream patch already installed, skipping.")
        
        
        
        
        # 2) Patch the seam that stream_ts uses to build the response body
        #    (i.e., the generator factory used just before returning StreamingHttpResponse)
        from apps.proxy.ts_proxy import views

        if getattr(views, "_orig_create_stream_generator", None) is None:
            self.logger.info("TooManyStreams: --------------------------Installing create_stream_generator patch in views.")
            views._orig_create_stream_generator = views.create_stream_generator

            def _patched_create_stream_generator(channel_id, client_id, client_ip, client_user_agent, channel_initializing):

                is_maxed =_TMS_is_maxed(channel_id)
                print(f"TooManyStreams: is_maxed={is_maxed} for channel {channel_id}")
                if is_maxed:
                    print(f"TooManyStreams: TooManyStreams: channel {channel_id} is currently maxed; using custom generator.")
                    print(f"TooManyStreams: using _orig_create_stream_generator for channel {channel_id}")

                    # Swap in *your* generator when we just observed the maxed-out condition.
                    return plugin_create_stream_generator(
                        channel_id, client_id, client_ip, client_user_agent, channel_initializing
                    ).generate
                print(f"TooManyStreams: using _orig_create_stream_generator for channel {channel_id}")

            views.create_stream_generator = _patched_create_stream_generator
            self.logger.info("TooManyStreams: Patched create_stream_generator to return custom generator when maxed")
        else:
            self.logger.info("TooManyStreams: --------------------------create_stream_generator patch already installed, skipping.")

    def install_generate_stream_url_override(self):
        # 1) Patch the function in its home module
        from apps.proxy.ts_proxy import url_utils  # adjust if needed

        if getattr(url_utils, "_orig_generate_stream_url", None) is None:
            print("Installing generate_stream_url patch in url_utils")

            url_utils._orig_generate_stream_url = url_utils.generate_stream_url

            def _patched_generate_stream_url(channel_id, *args, **kwargs):
                # your custom condition:
                if _TMS_is_maxed(channel_id):  # your helper
                    print("TMS: channel %s maxed -> returning custom URL", channel_id)
                    stream_url = f"/too_many_streams/{channel_id}/stream.ts"
                    stream_user_agent = "TooManyStreams-Agent/1.0"
                    transcode = False
                    profile_value = None
                    return stream_url, stream_user_agent, transcode, profile_value

                # otherwise fall through to the original
                return url_utils._orig_generate_stream_url(channel_id, *args, **kwargs)

            url_utils.generate_stream_url = _patched_generate_stream_url
        else:
            print("generate_stream_url patch already installed in url_utils; skipping base patch")

        # 2) Rebind the name at the call site if it was imported as a local symbol
        #    e.g. the module that defines stream_ts did: `from .url_utils import generate_stream_url`
        try:
            # Replace this import path with the actual module that defines `stream_ts`
            # (often something like apps.proxy.ts_proxy.views or apps.streams.views)
            from apps.proxy.ts_proxy import views as ts_views

            if getattr(ts_views, "generate_stream_url", None) is getattr(url_utils, "_orig_generate_stream_url", object()):
                ts_views.generate_stream_url = url_utils.generate_stream_url
                print("Rebound views.generate_stream_url to patched version")
            else:
                # Even if it doesn't match exactly, you can force-rebind:
                # ts_views.generate_stream_url = url_utils.generate_stream_url
                print("views.generate_stream_url was not the original; not rebinding")
        except Exception:
            print("Could not rebind call-site generate_stream_url; verify module path")


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
                    if isinstance(resp, JsonResponse) and resp.status_code == 502 and stream_valid:
                        # Build inputs for your generator
                        print("TMS: substituting StreamingHttpResponse for channel=%s (was 503 JSON).", channel_id)
                        client_ip = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                                    or request.META.get("REMOTE_ADDR"))
                        client_user_agent = request.META.get("HTTP_USER_AGENT")
                        client_id = getattr(request, "client_id", None) or f"tms-{int(time.time()*1000)}"
                        channel_initializing = True  # good default since we’re substituting init wait

                        gen_factory = plugin_create_stream_generator(
                            channel_id, client_id, client_ip, client_user_agent, channel_initializing
                        )
                        stream_resp = StreamingHttpResponse(gen_factory(), content_type="video/mp2t")
                        stream_resp["Cache-Control"] = "no-cache"
                        print(f"TMS: substituting StreamingHttpResponse {stream_resp.streaming} for channel {channel_id}")
                        print(f"TMS: substituting StreamingHttpResponse {stream_resp.status_code} for channel {channel_id}")
                        # print(f"TMS: substituting StreamingHttpResponse {len(stream_resp.streaming_content.)} for channel {channel_id}")
                        for cont in stream_resp.streaming_content:
                            print(f"TMS: streaming_content yields {len(cont)} bytes")
                        return stream_resp
                except Exception as e:
                    print(f"TMS: failed to substitute streaming response; using original 503 JSON. {e}")

                return resp

            ts_views.stream_ts = _patched_stream_ts
            print("TMS: installed stream_ts return override.")
        else:
            print("TMS: stream_ts override already installed; skipping.")
