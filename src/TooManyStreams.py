#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import os
import time
import pickle
import os, shutil, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apps.channels.models import Channel, ChannelStream, Stream
from apps.proxy.ts_proxy.server import ProxyServer
from apps.proxy.ts_proxy.services.channel_service import ChannelService
from core.utils import RedisClient

from .TooManyStreamsConfig import TooManyStreamsConfig
from .exceptions import TMS_CustomStreamNotFound
from .ActiveStreamImgGen import ActiveStreamImgGen


logger = logging.getLogger('plugins.too_many_streams.TooManyStreams')
logger.setLevel(os.environ.get("TMS_LOG_LEVEL", os.environ.get("DISPATCHARR_LOG_LEVEL", "INFO")).upper())
logger.info("TooManyStreams plugin initialized.")

class TooManyStreams:

    STREAM_NAME = 'TooManyStreams'
    # Timeout for how long to keep the "maxed out" flag (seconds)
    TMS_MAXED_TTL_SEC = 30
    # How many times a channel can hit maxed-out before we stop adding the TMS stream
    TMS_MAXED_COUNTER = 1
    # Path to store the "maxed out" flags (pickle file)
    TMS_MAXED_PKL = "/dev/shm/TMS/mark_maxed.pkl"
    # TS chunk size to read/send
    CHUNK = 188 * 7  # 1316 is fine; larger also OK


    @staticmethod
    def check_requirements_met() -> bool:
        """
        Checks if the requirements for TooManyStreams are met.
        Requirements:
           - pip install -r requirements.txt
        Returns:
            bool: True if requirements are met, False otherwise.
        """
        try:
           ActiveStreamImgGen()._find_wkhtmltoimage()
           return True
        except ImportError:
            logger.error("TooManyStreams: Missing required packages.")
            return False

    @staticmethod
    def install_requirements() -> None:
        """
        Installs the requirements for TooManyStreams via pip.
        """
        try:
            import subprocess
            subprocess.check_call(["pip", "install", "-r", os.path.join(os.path.dirname(__file__), "requirements.txt")])
            logger.info("TooManyStreams: Successfully installed required packages.")
            # Hold important deps like ffmpeg so the below wont break them
            subprocess.check_call(["apt-get", "update"])
            subprocess.check_call(["apt-get", "install", "-y", "wkhtmltopdf"])

        except Exception as e:
            logger.error(f"TooManyStreams: Failed to install required packages: {e}")

    @staticmethod
    def get_stream() -> Stream:
        """
        Finds and returns the custom TooManyStreams stream object.
        Raises an exception if not found.
        Returns:
            Stream: The custom TooManyStreams stream object.
        """
        stream:dict = Stream.objects.values('id', 'name', 'url').filter(
            name=TooManyStreams.STREAM_NAME, url=TooManyStreamsConfig.get_stream_url())
        
        if not stream:
            raise TMS_CustomStreamNotFound(f"TooManyStreams: No stream found with the criteria of:\
                            - name={TooManyStreams.STREAM_NAME},\
                            - url={TooManyStreamsConfig.get_stream_url()}")
        
        # Having multiple streams with same name/url is not ideal, but just use the first one
        if len(stream) > 1:
            logger.warning(f"TooManyStreams: Multiple streams found with the criteria of:\
                            - name={TooManyStreams.STREAM_NAME},\
                            - url={TooManyStreamsConfig.get_stream_url()}. Using the first one.")
        
        logger.debug(f"TooManyStreams: Fetching stream with : {stream[0]}")
        custom_stream = Stream.objects.get(id=stream[0]['id'])
        logger.debug(f"TooManyStreams: Found and using stream: {custom_stream}")
        return custom_stream
    
    @staticmethod
    def create_stream() -> Stream:
        """
        Creates the custom TooManyStreams stream if it does not already exist.
        """
        custom_stream = None
        # Data required to create the custom stream
        data = {
            'name': TooManyStreams.STREAM_NAME,
            'url': TooManyStreamsConfig.get_stream_url(),
            'is_custom': True,
            # Other data required for Stream creation. Defaults / nulls are used here.
            'channel_group': None,
            'stream_profile_id': None,
        }

        # Check if the stream already exists
        try:
            custom_stream = TooManyStreams.get_stream() 
            logger.info(f"TooManyStreams: Custom stream already exists: {custom_stream}")
            return custom_stream
        except TMS_CustomStreamNotFound: # TooManyStreams.get_stream() raises this if not found
            logger.info("TooManyStreams: Custom stream not found; creating it.")

        # Create the custom stream
        custom_stream = Stream.objects.create(**data) 
        logger.info(f"TooManyStreams: Created custom stream: {custom_stream}")

        return custom_stream

    @staticmethod
    def delete_stream() -> None:
        """
        Deletes the custom TooManyStreams stream if it exists.
        """
        try:
            custom_stream = TooManyStreams.get_stream()
            custom_stream.delete()
            logger.info(f"TooManyStreams: Deleted custom stream: {custom_stream}")
        except TMS_CustomStreamNotFound:
            logger.info("TooManyStreams: Custom stream not found; nothing to delete.")

    @staticmethod
    def get_or_create_stream() -> Stream:
        """
        Gets the custom TooManyStreams stream, creating it if it does not exist.
        """
        try:
            return TooManyStreams.get_stream()
        except TMS_CustomStreamNotFound:
            return TooManyStreams.create_stream()

    @staticmethod
    def add_stream_to_channel(channel_id:int) -> None:
        """
        Adds the custom TooManyStreams stream to the specified channel.
        """
        custom_stream = TooManyStreams.get_or_create_stream()
        channel = None
        try:
            channel = Channel.objects.get(id=channel_id)
        except Channel.DoesNotExist:
            logger.error(f"TooManyStreams: Channel with ID {channel_id} does not exist.")
            return

        if not channel:
            logger.error(f"TooManyStreams: Channel with ID {channel_id} could not be fetched.")
            return

        all_streams:list[Stream] = channel.streams.all().order_by("channelstream__order")
        if custom_stream in all_streams:
            logger.info(f"TooManyStreams: Stream already assigned to channel {channel_id}.")
            return

        ChannelStream.objects.create(
                        channel=channel, stream_id=custom_stream.id, order=9999
                    )
        logger.info(f"TooManyStreams: Added stream {custom_stream.id} to channel {channel_id}.")

    @staticmethod   
    def remove_stream_from_channel(channel_id:int) -> None:
        """
        Removes the custom TooManyStreams stream from the specified channel.
        """
        custom_stream = TooManyStreams.get_or_create_stream()
        channel = None
        try:
            channel = Channel.objects.get(id=channel_id)
        except Channel.DoesNotExist:
            logger.error(f"TooManyStreams: Channel with ID {channel_id} does not exist.")
            return
        
        if not channel:
            logger.error(f"TooManyStreams: Channel with ID {channel_id} could not be fetched.")
            return

        if custom_stream not in channel.streams.all():
            logger.info(f"TooManyStreams: Stream not assigned to channel {channel_id}.")
            return
        
        channel.streams.remove(custom_stream.id)
        channel.save()

        # Stopping channel 
        # retry 3 times. # Below code is from Dispatcharr\apps\proxy\ts_proxy\views.py stop_channel()
        proxy_server = ProxyServer.get_instance()
        for _ in range(5):
            try:

                result = ChannelService.stop_channel(str(channel.uuid))
                proxy_server.stop_channel(channel.uuid)
                
                logger.debug(f"TooManyStreams: ProxyServer stopped channel {channel_id}.")
                time.sleep(1)

                if result.get("status") == "error":
                    logger.warning(f"TooManyStreams: Failed to stop channel {channel_id}: {result.get('message')}")
                    continue
                else:
                    logger.info(f"TooManyStreams: Stopped channel {channel_id} successfully.")
            except Exception as e:
                logger.error(f"TooManyStreams: Failed to stop stream for channel {channel_id}: {e}")
                time.sleep(1)


        logger.info(f"TooManyStreams: Removed stream {custom_stream.id} from channel {channel_id}.")

    @staticmethod
    def get_maxed_data() -> dict:
        """
        Loads and returns the dictionary of channels recently marked as maxed-out.
        Returns:
            dict: Dictionary of channel_id to maxed info.
        """
        _tms_last_maxed:dict = {}
        if os.path.exists(TooManyStreams.TMS_MAXED_PKL):
            try:
                _tms_last_maxed = pickle.load(open(TooManyStreams.TMS_MAXED_PKL, "rb"))
                logger.debug(f"TooManyStreams: Loaded maxed info: {TooManyStreams.TMS_MAXED_PKL}")
            except Exception:
                logger.warning(f"TooManyStreams: Failed to load maxed info from: {TooManyStreams.TMS_MAXED_PKL}. Returning empty dict.")
                pass
        return _tms_last_maxed

    @staticmethod
    def mark_streams_maxed(channel_id) -> None:
        """
        Marks the specified channel as having maxed-out streams. Adds a short-lived flag.
        """
        channel_id = str(channel_id)
        logger.info(f"TooManyStreams: Marking channel {channel_id} as maxed")
        # Set a short-lived flag that this channel recently hit maxed-out streams
        _exp_time = time.time() + TooManyStreams.TMS_MAXED_TTL_SEC
        _tms_last_maxed:dict = TooManyStreams.get_maxed_data()
        if channel_id not in _tms_last_maxed:
            _tms_last_maxed[channel_id] = {"exp_time": _exp_time, "failed_counter": 1}
        else:
            _tms_last_maxed[channel_id].update({"exp_time": _exp_time, "failed_counter": _tms_last_maxed[channel_id]["failed_counter"] + 1})
        _pkl_path = os.path.dirname(TooManyStreams.TMS_MAXED_PKL)
        if not os.path.exists(_pkl_path):
            os.makedirs(_pkl_path, exist_ok=True)

        pickle.dump(_tms_last_maxed, open(TooManyStreams.TMS_MAXED_PKL, "wb"))


        logger.debug(f"TooManyStreams: Marked channel {channel_id} as maxed until {_tms_last_maxed[channel_id]}")

    @staticmethod
    def is_streams_maxed(channel_id) -> bool:
        """
        Checks if the specified channel is currently marked as having maxed-out streams.
        Adds the TooManyStreams stream to the channel if maxed, removes it if not.
        Returns:
            bool: True if the channel is marked as maxed, False otherwise.
        """
        is_maxed:bool = False
        channel_id = str(channel_id)
        _tms_last_maxed:dict = TooManyStreams.get_maxed_data()

        if maxed := _tms_last_maxed.get(channel_id, None):

            _exp_time = maxed.get("exp_time", None)
            _failed_counter = maxed.get("failed_counter", None)

            if _exp_time is None or _failed_counter is None:
                logger.debug(f"TooManyStreams: Channel {channel_id},_exp_time: {_exp_time}, _failed_counter: {_failed_counter} invalid.")
                is_maxed = False
            elif _exp_time < time.time():
                logger.info(f"TooManyStreams: Channel {channel_id} maxed flag expired at {_exp_time}; removing.")
                _tms_last_maxed.pop(channel_id, None)
                pickle.dump(_tms_last_maxed, open(TooManyStreams.TMS_MAXED_PKL, "wb"))
                is_maxed = False
            elif _failed_counter < TooManyStreams.TMS_MAXED_COUNTER:
                logger.debug(f"TooManyStreams: Channel {channel_id} has only {_failed_counter} failed attempts; below threshold of {TooManyStreams.TMS_MAXED_COUNTER}. Not marking as maxed.")
                is_maxed = False
            else:
                is_maxed = True
            
        else:
            logger.info(f"TooManyStreams: Channel {channel_id} has no maxed info")
            is_maxed = False
        
        if is_maxed:
            logger.debug(f"TooManyStreams: Channel {channel_id} is currently marked as maxed")
            TooManyStreams.add_stream_to_channel(channel_id)
        else:
            logger.debug(f"TooManyStreams: Channel {channel_id} is NOT marked as maxed")
            TooManyStreams.remove_stream_from_channel(channel_id)

        return is_maxed
    
    @staticmethod
    def start_maxed_channel_cleanup_thread():
        """
        Periodically cleans up expired maxed-out flags from the pickle file.
        """
        logger.info("TooManyStreams: Starting maxed channel cleanup thread.")
        def _cleanup_thread():
            while True:
                logger.debug("TooManyStreams: Cleanup thread running.")
                _tms_last_maxed:dict = TooManyStreams.get_maxed_data()
                logger.debug(f"TooManyStreams: Cleanup loaded maxed data: {_tms_last_maxed}")
                for channel_id in list(_tms_last_maxed.keys()):
                    TooManyStreams.is_streams_maxed(channel_id)  # This will remove expired entries
                    logger.debug(f"TooManyStreams: Cleanup checked channel {channel_id}")
                # !!WARNING: if the stream length is shorter then the cleanup interval, Dispatcharr can go into an infinite loop of reconnects / channel switches.
                time.sleep(TooManyStreams.TMS_MAXED_TTL_SEC)
        threading.Thread(target=_cleanup_thread, daemon=True).start()
        logger.info("TooManyStreams: Started maxed channel cleanup thread.")


    @staticmethod
    def install_get_stream_override():
        # Import the class that owns get_stream
        from apps.channels.models import Channel 
        
        if getattr(Channel, "_orig_get_stream", None) is None:
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
                    # ### TooManyStreams logic here ###
                    # TooManyStreams.is_streams_maxed(self.id)
                    # ### TooManyStreams END logic here ###
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
                    if not TooManyStreams.is_streams_maxed(self.id):
                        error_reason = "All M3U profiles have reached maximum connection limits" 
                        TooManyStreams.mark_streams_maxed(self.id)
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

    @staticmethod
    def apply_to_all_channels():
        """
        Applies the TooManyStreams logic to all channels in the database.
        """
        channels = Channel.objects.all()
        for channel in channels:
            TooManyStreams.add_stream_to_channel(channel.id)
            logger.info(f"TooManyStreams: Applied to channel {channel.id}")

    @staticmethod
    def remove_from_all_channels():
        """
        Removes the TooManyStreams stream from all channels in the database.
        """
        channels = Channel.objects.all()
        for channel in channels:
            TooManyStreams.remove_stream_from_channel(channel.id)
            logger.info(f"TooManyStreams: Removed from channel {channel.id}")


    @staticmethod
    def stream_still_mpegts_http_thread(
        image_path: str|None = None,
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

        if image_path and not os.path.exists(image_path):
            logger.error(f"TooManyStreams: Image path {image_path} does not exist.")

        ffmpeg_bin = ffmpeg_or_die()

        # Encoding defaults tuned for compatibility + quick startup for a still image
        fps = 1               # 1 fps. Is still image
        v_bitrate = "800k"
        a_bitrate = "96k"
        muxrate   = "900k"
        bufsize   = "1600k"

        # Pre-detect encoders once (best-effort)
        def encoder_available(name: str) -> bool:
            try:
                out = subprocess.check_output([ffmpeg_bin, "-v", "0", "-hide_banner", "-encoders"], text=True)
                return f" {name} " in out
            except Exception:
                return False

        use_aac  = encoder_available("aac")

        # Build the command for a given image
        def make_ffmpeg_cmd(img: str, stream_ts:str):

            # !!WARNING: if the stream length is shorter then the cleanup interval, Dispatcharr can go into an infinite loop of reconnects / channel switches.
            stream_length_secs = TooManyStreams.TMS_MAXED_TTL_SEC * 2
            in_args = [
                "-loop","1","-framerate",str(fps),"-i",img,
                "-f","lavfi","-i","anullsrc=r=48000:cl=stereo",
                "-c:v","libx264","-preset","ultrafast","-tune","stillimage","-r",str(fps),"-g",str(fps),"-keyint_min",str(fps),
                "-b:v",v_bitrate,"-maxrate",v_bitrate,"-minrate",v_bitrate,"-bufsize",bufsize,
                "-c:a","aac" if use_aac else "mp2","-b:a",a_bitrate,
                "-muxrate",muxrate,"-fflags","+genpts", "-mpegts_flags", "+resend_headers+initial_discontinuity", "-t", f"{stream_length_secs}", "-f","mpegts", stream_ts
            ]

            # Return the full command
            return [ffmpeg_bin, *in_args]


        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                if self.path not in ("/", "/stream.ts"):
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Not found")
                    return


                # Send headers first so VLC starts reading
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                # keep-alive fine; the stream is indefinite
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                # Start ffmpeg for this client
                stream_ts = os.path.join(os.path.dirname(__file__), "no_streams.ts")
                if os.path.exists(stream_ts):
                    os.remove(stream_ts)

                chosen_img = image_path
                # Generate the image from 
                if not image_path or not os.path.exists(image_path):
                    chosen_img = os.path.join(os.path.dirname(__file__), "too_many_streams2.jpg")
                
                    try:
                        asig = ActiveStreamImgGen(out_path=chosen_img)
                        asig.get_active_streams()
                        asig.generate()
                    except Exception as e:
                        logger.error(f"TMS ERROR: [HTTP] Client {self.client_address} image generation error: {e}", file=sys.stderr)
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(b"Failed to generate image")
                        return


                cmd = make_ffmpeg_cmd(chosen_img, stream_ts)
                logger.debug(f"Running ffmpeg command: {' '.join(cmd)}")
                # # Generate the stream
                gen_ts = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                gen_ts.wait()  # wait for process to complete
                if gen_ts.returncode != 0:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"Failed to generate stream")
                    return
                else:
                    gen_ts.terminate()
                if not os.path.exists(stream_ts):
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"Failed to generate stream")
                    return
                try:
                    with open(stream_ts, 'rb') as f:
                        while True:
                            CHUNK = 1316 * 32  # bigger writes help downstream
                            buf = f.read(CHUNK)
                            if not buf:
                                break
                            try:
                                self.wfile.write(buf)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                break
                except Exception as e:
                    logger.error(f"TMS ERROR: [HTTP] Client {self.client_address} stream error: {e}", file=sys.stderr)
                logger.debug(f"TMS ERROR: [HTTP] Client {self.client_address} disconnected")

            def log_message(self, fmt, *args):
                # Quieter server logs
                return

        httpd = ThreadingHTTPServer((host, port), Handler)
        logger.info(f"HTTP MPEG-TS server listening on http://{host}:{port}/stream.ts")

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("\nStopping server…")
            httpd.shutdown()
            httpd.server_close()


        