#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import os
import threading
import re
import time

from apps.channels.models import Channel, ChannelStream, Stream
from apps.proxy.ts_proxy.server import ProxyServer
from apps.proxy.ts_proxy.services.channel_service import ChannelService
from core.utils import RedisClient

from .TooManyStreamsConfig import TooManyStreamsConfig
from .exceptions import TMS_CustomStreamNotFound
from .StreamServer import StreamServer

logger = logging.getLogger('plugins.too_many_streams.TooManyStreams')
logger.setLevel(os.environ.get("TMS_LOG_LEVEL", os.environ.get("DISPATCHARR_LOG_LEVEL", "INFO")).upper())

class TooManyStreams:

    STREAM_NAME = 'TooManyStreams'
    TMS_MAXED_TTL_SEC = 30
    TMS_MAXED_COUNTER = 1
    
    REFRESH_SIGNAL = threading.Event()
    _stream_manager_filter_installed = False

    class _TmsStreamInfoFilter(logging.Filter):
        """
        Suppress noisy stream-manager info lines only for active TooManyStreams channels.
        Ignores logs that spam: 
            2026-03-15 03:43:01,741 INFO ts_proxy.stream_manager Stream info for channel ded35132-7950-4e82-b628-60eecad7ce05: [aist#0:1/aac @ 0x562b7fcf8040] timestamp discontinuity (stream id=257): 35240067, new offset= -93629957761
        """

        _MSG_PATTERN = re.compile(r"Stream info for channel ([^:]+):")

        def __init__(self, ttl_sec: int = 10):
            super().__init__()
            self.ttl_sec = ttl_sec
            self._cache = {}

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
                if "Stream info for channel " not in msg:
                    return True

                match = self._MSG_PATTERN.search(msg)
                if not match:
                    return True

                channel_id = match.group(1)
                if self._is_tms_channel(channel_id):
                    return False
            except Exception:
                # Never block logs if filter logic fails.
                return True
            return True

        def _is_tms_channel(self, channel_id: str) -> bool:
            now = time.time()
            cached = self._cache.get(channel_id)
            if cached and cached[0] > now:
                return cached[1]

            is_tms = False
            try:
                redis_client = RedisClient.get_client()
                metadata_key = f"ts_proxy:channel:{channel_id}:metadata"
                stream_id_raw = redis_client.hget(metadata_key, "stream_id")
                if stream_id_raw:
                    stream_id = int(stream_id_raw.decode("utf-8") if isinstance(stream_id_raw, bytes) else stream_id_raw)
                    stream_url = Stream.objects.filter(id=stream_id).values_list("url", flat=True).first()
                    is_tms = bool(stream_url and stream_url == TooManyStreamsConfig.get_stream_url())
            except Exception:
                is_tms = False

            self._cache[channel_id] = (now + self.ttl_sec, is_tms)
            return is_tms

    @staticmethod
    def install_stream_manager_log_filter():
        if TooManyStreams._stream_manager_filter_installed:
            return

        target_logger = logging.getLogger("ts_proxy.stream_manager")
        target_logger.addFilter(TooManyStreams._TmsStreamInfoFilter())
        TooManyStreams._stream_manager_filter_installed = True
        logger.info("TooManyStreams: Installed ts_proxy.stream_manager filter for TMS stream-info noise")

    @staticmethod
    def check_requirements_met() -> bool:
        return True

    @staticmethod
    def install_requirements() -> None:
        try:
            import subprocess
            subprocess.check_call(["pip", "install", "-r", os.path.join(os.path.dirname(__file__), "..", "requirements.txt")])
            logger.info("TooManyStreams: Installed requirements.")
        except Exception as e:
            logger.error(f"TooManyStreams: Failed to install requirements: {e}")

    @staticmethod
    def get_stream() -> Stream:
        stream:dict = Stream.objects.values('id', 'name', 'url').filter(
            name=TooManyStreams.STREAM_NAME, url=TooManyStreamsConfig.get_stream_url())
        if not stream:
            raise TMS_CustomStreamNotFound("TooManyStreams: Stream not found.")
        return Stream.objects.get(id=stream[0]['id'])
    
    @staticmethod
    def get_or_create_stream() -> Stream:
        try:
            return TooManyStreams.get_stream()
        except TMS_CustomStreamNotFound:
            data = {
                'name': TooManyStreams.STREAM_NAME,
                'url': TooManyStreamsConfig.get_stream_url(),
                'is_custom': True,
                'channel_group': None,
                'stream_profile_id': None,
            }
            return Stream.objects.create(**data)

    @staticmethod
    def add_stream_to_channel(channel_id:int) -> None:
        custom_stream = TooManyStreams.get_or_create_stream()
        try:
            channel = Channel.objects.get(id=channel_id)
            if custom_stream not in channel.streams.all():
                ChannelStream.objects.create(channel=channel, stream_id=custom_stream.id, order=9999)
        except Exception as e:
            logger.error(
                "TooManyStreams: Failed to add custom stream to channel %s: %s",
                channel_id,
                e,
                exc_info=True,
            )

    @staticmethod   
    def remove_stream_from_channel(channel_id:int) -> None:
        custom_stream = TooManyStreams.get_or_create_stream()
        try:
            channel = Channel.objects.get(id=channel_id)
            if custom_stream in channel.streams.all():
                channel.streams.remove(custom_stream.id)
                channel.save()
                proxy_server = ProxyServer.get_instance()
                ChannelService.stop_channel(str(channel.uuid))
                proxy_server.stop_channel(channel.uuid)
        except Exception as e:
            logger.error(
                "TooManyStreams: Failed to remove custom stream from channel %s: %s",
                channel_id,
                e,
                exc_info=True,
            )

    @staticmethod
    def mark_streams_maxed(channel_id) -> None:
        channel_id = str(channel_id)
        redis_client = RedisClient.get_client()
        key = f"tms:maxed_out:{channel_id}"
        redis_client.incr(key)
        redis_client.expire(key, TooManyStreams.TMS_MAXED_TTL_SEC)

    @staticmethod
    def is_streams_maxed(channel_id) -> bool:
        channel_id = str(channel_id)
        redis_client = RedisClient.get_client()
        key = f"tms:maxed_out:{channel_id}"
        try:
            val = int(redis_client.get(key) or 0)
        except: val = 0
        
        is_maxed = val >= TooManyStreams.TMS_MAXED_COUNTER
        if is_maxed: TooManyStreams.add_stream_to_channel(channel_id)
        else: TooManyStreams.remove_stream_from_channel(channel_id)
        return is_maxed

    @staticmethod
    def trigger_refresh():
        TooManyStreams.REFRESH_SIGNAL.set()

    @staticmethod
    def install_get_stream_override():
        from apps.channels.models import Channel 
        if getattr(Channel, "_orig_get_stream", None) is None:
            Channel._orig_get_stream = Channel.get_stream
            
            def _wrapped_get_stream(self, *args, **kwargs):
                redis_client = RedisClient.get_client()
                error_reason = None

                if not self.streams.exists():
                    return None, None, "No streams assigned to channel"

                # 1. Check if a stream is already active for this channel (Restore session logic)
                stream_id_bytes = redis_client.get(f"channel_stream:{self.id}")
                if stream_id_bytes:
                    try:
                        stream_id = int(stream_id_bytes)
                        profile_id_bytes = redis_client.get(f"stream_profile:{stream_id}")
                        if profile_id_bytes:
                            return stream_id, int(profile_id_bytes), None
                    except (ValueError, TypeError): pass

                # 2. Try to find an available stream
                has_streams_but_maxed_out = False
                has_active_profiles = False

                for stream in self.streams.all().order_by("channelstream__order"):
                    m3u_account = stream.m3u_account
                    if not m3u_account: continue

                    profiles = m3u_account.profiles.all()
                    # Ensure default profile is checked first
                    sorted_profiles = sorted(profiles, key=lambda x: not x.is_default)

                    for profile in sorted_profiles:
                        if not profile.is_active: continue
                        has_active_profiles = True

                        profile_connections_key = f"profile_connections:{profile.id}"
                        current_connections = int(redis_client.get(profile_connections_key) or 0)

                        if profile.max_streams == 0 or current_connections < profile.max_streams:
                            redis_client.set(f"channel_stream:{self.id}", stream.id)
                            redis_client.set(f"stream_profile:{stream.id}", profile.id)
                            if profile.max_streams > 0: redis_client.incr(profile_connections_key)
                            
                            TooManyStreams.trigger_refresh()
                            return stream.id, profile.id, None
                        else:
                            has_streams_but_maxed_out = True

                # 3. Handle maxed out scenario
                if has_streams_but_maxed_out:
                    if not TooManyStreams.is_streams_maxed(self.id):
                        TooManyStreams.mark_streams_maxed(self.id)
                        return None, None, "All M3U profiles have reached maximum connection limits"
                    
                    # Return our custom stream
                    try:
                        custom_stream = TooManyStreams.get_stream()
                        return custom_stream.id, None, None
                    except: pass

                error_reason = "No compatible profile found" if has_active_profiles else "No active profiles found"
                return None, None, error_reason

            Channel.get_stream = _wrapped_get_stream

    @staticmethod
    def apply_to_all_channels():
        for c in Channel.objects.all(): TooManyStreams.add_stream_to_channel(c.id)

    @staticmethod
    def remove_from_all_channels():
        for c in Channel.objects.all(): TooManyStreams.remove_stream_from_channel(c.id)

    @staticmethod
    def stream_still_mpegts_http_thread(image_path=None, host="127.0.0.1", port=8081):
        server = StreamServer(host=host, port=port, image_path=image_path, refresh_signal=TooManyStreams.REFRESH_SIGNAL)
        server.start()
