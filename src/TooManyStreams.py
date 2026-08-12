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

    # How the PROXY should read our card back off :1337. A channel's own profile
    # is tuned for real sources -- on prod that's an h264_nvenc 1080p 7Mbit
    # transcode -- and applying it to the card costs an NVENC session and, far
    # worse, seconds of ffmpeg probing. ffmpeg's -analyzeduration counts STREAM
    # time and -re pins that to wall-clock, so the default 5s probe literally
    # cost 5s of viewer wait; when it overran the channel profile's -rw_timeout,
    # ffmpeg reported "Connection timed out" and retried, which is where a cold
    # fall-to-card spent 12-47s. Our card's format is fixed and known, so a
    # modest probe is safe, and -c copy keeps the card off the GPU entirely.
    # (See also CARD_FPS in StreamServer -- the probe budget and the card's
    # frame rate have to be chosen together.)
    CARD_PROFILE_NAME = 'TMS Card (copy, fast probe)'
    CARD_PROFILE_COMMAND = 'ffmpeg'
    # +discardcorrupt is load-bearing, not defensive dressing: restarting the
    # card encoder (new content) resets its MPEG-TS timestamps to zero, and a
    # -c copy reader sees that as a backward DTS jump -- "Packet corrupt ...
    # corrupt input packet" -- and drops the stream. Re-encoding used to mask
    # this; copying does not. Pair it with the restart rate-limit in
    # StreamServer, which keeps those discontinuities rare in the first place.
    CARD_PROFILE_PARAMETERS = (
        '-probesize 256k -analyzeduration 1000000 -fflags +genpts+discardcorrupt '
        '-user_agent {userAgent} -i {streamUrl} -c copy -f mpegts pipe:1'
    )

    REFRESH_SIGNAL = threading.Event()
    _stream_manager_filter_installed = False
    _card_profile_id = None
    _card_stream_id = None

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
    def get_card_stream_id():
        """Id of the card Stream row, cached -- this sits on the tune hot path."""
        if TooManyStreams._card_stream_id is None:
            try:
                TooManyStreams._card_stream_id = TooManyStreams.get_stream().id
            except Exception:
                return None  # not created yet; don't cache the miss
        return TooManyStreams._card_stream_id

    @staticmethod
    def get_card_stream_profile():
        """The StreamProfile the proxy should use while it is reading our card.

        Created once, then left alone: it shows up in the Dispatcharr UI like any
        other profile, so an operator who tunes it keeps their edit.
        """
        from core.models import StreamProfile

        if TooManyStreams._card_profile_id is not None:
            profile = StreamProfile.objects.filter(id=TooManyStreams._card_profile_id).first()
            if profile:
                return profile
            TooManyStreams._card_profile_id = None  # deleted underneath us

        profile, created = StreamProfile.objects.get_or_create(
            name=TooManyStreams.CARD_PROFILE_NAME,
            defaults={
                'command': TooManyStreams.CARD_PROFILE_COMMAND,
                'parameters': TooManyStreams.CARD_PROFILE_PARAMETERS,
                'is_active': True,
            },
        )
        if created:
            logger.info("TooManyStreams: created stream profile '%s'", TooManyStreams.CARD_PROFILE_NAME)
        TooManyStreams._card_profile_id = profile.id
        return profile

    @staticmethod
    def _is_serving_card(channel) -> bool:
        """Is this channel currently pointed at the card?

        get_stream() runs before get_stream_profile() on the same Channel
        instance (ts_proxy/url_utils.py:85 then :114), so the flag it leaves
        behind is the cheapest possible answer. Other call sites can reach
        get_stream_profile() without that, so fall back to the Redis reservation.
        """
        flag = getattr(channel, "_tms_serving_card", None)
        if flag is not None:
            return flag

        card_id = TooManyStreams.get_card_stream_id()
        if card_id is None:
            return False
        try:
            stream_id_bytes = RedisClient.get_client().get(f"channel_stream:{channel.id}")
            return bool(stream_id_bytes) and int(stream_id_bytes) == card_id
        except Exception:
            return False

    @staticmethod
    def install_get_stream_profile_override():
        """Serve the card with a cheap, fast-probing profile instead of the channel's."""
        from apps.channels.models import Channel

        if getattr(Channel, "_orig_get_stream_profile", None) is not None:
            return
        Channel._orig_get_stream_profile = Channel.get_stream_profile

        def _wrapped_get_stream_profile(self):
            if TooManyStreams._is_serving_card(self):
                try:
                    profile = TooManyStreams.get_card_stream_profile()
                    if profile and profile.is_active:
                        return profile
                except Exception as e:
                    logger.warning(
                        "TooManyStreams: could not resolve the card profile, using the channel's: %s", e
                    )
            return Channel._orig_get_stream_profile(self)

        Channel.get_stream_profile = _wrapped_get_stream_profile
        logger.info("TooManyStreams: installed get_stream_profile override for the card.")

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
        """Has this channel hit its connection limit recently?

        This runs inside get_stream, on the tune path, so it only reads. It used to
        also tidy membership: add the card when maxed, and -- the dangerous half --
        remove_stream_from_channel when not, which does stop_channel +
        proxy_server.stop_channel. A predicate that stops channels is a landmine
        across ~2806 of them, and it only stayed harmless because the branch is
        unreachable in the current layout (the card sits at order 9999 with an
        unlimited profile, so the selection loop returns it before we ever get here).
        Membership is owned by the explicit apply/remove actions instead. The add is
        kept -- it is how the card reaches a channel for installs that have not
        applied it fleet-wide -- and a card row left behind is inert, since the loop
        only reaches order 9999 when nothing real is free.
        """
        channel_id = str(channel_id)
        redis_client = RedisClient.get_client()
        key = f"tms:maxed_out:{channel_id}"
        try:
            val = int(redis_client.get(key) or 0)
        except: val = 0

        is_maxed = val >= TooManyStreams.TMS_MAXED_COUNTER
        if is_maxed: TooManyStreams.add_stream_to_channel(channel_id)
        return is_maxed

    @staticmethod
    def reserve_profile_slot(profile, redis_client) -> bool:
        """Claim one connection slot on `profile`. Returns False if it is full.

        Atomic on purpose. The old form -- GET, compare, SET, INCR -- let concurrent
        tunes all read the same pre-INCR count and admit past max_streams during a
        failover wave. INCR-then-check-and-roll-back is what Dispatcharr core does
        (_check_and_reserve_profile_slot, apps/channels/models.py ~408), so mirroring
        it also keeps our accounting shaped the way release_stream expects.
        """
        if profile.max_streams == 0:
            return True                       # unlimited -- never counted, never INCR'd

        key = f"profile_connections:{profile.id}"
        if redis_client.incr(key) <= profile.max_streams:
            return True
        redis_client.decr(key)                # lost the race; net zero
        return False

    @staticmethod
    def trigger_refresh():
        TooManyStreams.REFRESH_SIGNAL.set()

    @staticmethod
    def ordered_candidates(channel):
        """The channel's streams, best-first, with managed cards pinned last.

        Real sources are ordered by what they were last measured to be carrying
        (see QualityScanner); the channel's hand-ordering breaks ties and is the
        whole ordering for a channel nobody has scanned, so an unscanned install
        behaves exactly as it does today.

        Cards stay last unconditionally rather than being ranked. A card is the
        give-up path: if every real source is known bad we still want to *try* one,
        because a verdict can be stale or a slate can go live at kickoff, and
        showing a card to someone whose stream would have worked is worse than a
        few seconds of buffering.
        """
        pairs = [
            (cs.stream, cs.order)
            for cs in ChannelStream.objects.filter(channel=channel)
                                           .select_related("stream")
                                           .order_by("order")
        ]
        card_ids = {TooManyStreams.get_card_stream_id()}
        real = [(s, o) for s, o in pairs if s.id not in card_ids]
        cards = [s for s, o in pairs if s.id in card_ids]

        try:
            from .QualityScanner import QualityScanner
            ordered = QualityScanner.order_streams(real)
        except Exception as e:
            logger.warning("TooManyStreams: quality ranking unavailable, using channel order: %s", e)
            ordered = [s for s, _ in real]

        return ordered + cards

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
                            self._tms_serving_card = (stream_id == TooManyStreams.get_card_stream_id())
                            return stream_id, int(profile_id_bytes), None
                    except (ValueError, TypeError): pass

                # 2. Try to find an available stream
                has_streams_but_maxed_out = False
                has_active_profiles = False

                for stream in TooManyStreams.ordered_candidates(self):
                    m3u_account = stream.m3u_account
                    if not m3u_account: continue

                    profiles = m3u_account.profiles.all()
                    # Ensure default profile is checked first
                    sorted_profiles = sorted(profiles, key=lambda x: not x.is_default)

                    for profile in sorted_profiles:
                        if not profile.is_active: continue
                        has_active_profiles = True

                        # Reserve atomically, THEN publish the keys release_stream
                        # reads. Reserving first means a crash in between leaks at
                        # most one slot; publishing first would let a concurrent
                        # release DECR a slot we had not claimed yet.
                        if TooManyStreams.reserve_profile_slot(profile, redis_client):
                            redis_client.set(f"channel_stream:{self.id}", stream.id)
                            redis_client.set(f"stream_profile:{stream.id}", profile.id)

                            TooManyStreams.trigger_refresh()
                            # The card is reachable through THIS loop, not just the
                            # maxed branch below: it sits at order 9999 on an
                            # unlimited profile, so on a fleet-applied install the
                            # loop reaches it whenever nothing real is free. Hard-
                            # coding False here told get_stream_profile "not the
                            # card" while serving exactly that, and the copy profile
                            # was only still picked up because get_stream_profile
                            # happens to run on a re-fetched Channel whose flag is
                            # unset, falling through to the Redis check.
                            self._tms_serving_card = (stream.id == TooManyStreams.get_card_stream_id())
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
                        self._tms_serving_card = True
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
