"""Delegate allocation to Dispatcharr; add warning only at URL resolution.

A (warning_stream_id, None, None, False) Channel result is NOT supported by
Dispatcharr: generate_stream_url requires a real M3U profile. Consequently the
Channel hook is an observer only and preserves every native result verbatim.
"""
from contextvars import ContextVar
from functools import wraps
import inspect
import logging
import threading

from apps.channels.models import Channel, ChannelStream, Stream
from apps.m3u.connection_pool import (
    get_credential_connection_count,
    get_profile_connection_count,
)
from core.utils import RedisClient
from apps.proxy.live_proxy.redis_keys import RedisKeys
from apps.proxy.live_proxy.constants import ChannelMetadataField

from .TooManyStreamsConfig import TooManyStreamsConfig
from .StreamServer import StreamServer

logger = logging.getLogger('plugins.too_many_streams.TooManyStreams')


class TooManyStreams:
    STREAM_NAME = 'TooManyStreams'
    CAPACITY_ERROR = 'All active M3U profiles have reached maximum connection limits'
    SUPPORTED_VERSIONS = {'0.31.0', '0.33.0'}
    REFRESH_SIGNAL = threading.Event()
    _lock = threading.RLock()
    _server = None

    @staticmethod
    def check_requirements_met():
        """Reject unreviewed host contracts before installing any hooks."""
        from version import __version__
        if __version__ not in TooManyStreams.SUPPORTED_VERSIONS:
            raise RuntimeError(
                f'Unsupported Dispatcharr {__version__}; supported: 0.31.0, 0.33.0'
            )
        parameters = inspect.signature(Channel.get_stream).parameters
        if not getattr(Channel, '_tms_hook_state', None) and not {
            'requester', 'allowed_m3u_profiles'
        }.issubset(parameters):
            # Test doubles/wrappers can retain the contract through __wrapped__.
            if not any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
                raise RuntimeError('Unsupported Dispatcharr Channel.get_stream contract')
        if getattr(Channel, '_orig_get_stream', None):
            raise RuntimeError('Legacy TooManyStreams hook detected; restart Dispatcharr before enabling')
        return True

    @staticmethod
    def get_or_create_stream():
        # This row is for native proxy metadata only. NEVER attach it to channels
        # or reserve its automatically assigned Custom M3U account/profile.
        stream, _ = Stream.objects.get_or_create(
            name=TooManyStreams.STREAM_NAME,
            url=TooManyStreamsConfig.get_stream_url(),
            defaults={'is_custom': True, 'channel_group': None, 'stream_profile_id': None},
        )
        return stream

    @staticmethod
    def _confirmed_capacity(channel):
        """Read-only corroboration of the native capacity failure.

        Never reserve/release here. A full profile counter is sufficient;
        otherwise consult the native shared-credential counter. If capacity
        changed after native selection, preserve the native failure.
        """
        redis = RedisClient.get_client()
        # The native view can recover assignment/profile IDs from old keys, and
        # native teardown can release a profile from metadata. Never lend those
        # identities to an internal warning; ambiguous stale state fails closed.
        if (redis.get(f'channel_stream:{channel.id}') is not None
                or redis.hget(RedisKeys.channel_metadata(str(channel.uuid)),
                              ChannelMetadataField.M3U_PROFILE) is not None):
            return False
        found = False
        for stream in channel.streams.all().order_by('channelstream__order'):
            account = stream.m3u_account
            if not account or not account.is_active:
                continue
            profiles = list(account.profiles.filter(is_active=True))
            if not any(profile.is_default for profile in profiles):
                continue
            for profile in profiles:
                found = True
                if profile.max_streams <= 0:
                    return False
                if (get_profile_connection_count(profile, redis) < profile.max_streams
                        and get_credential_connection_count(profile, redis) < profile.max_streams):
                    return False
        return found

    @staticmethod
    def install_get_stream_override():
        """Install observer + URL wrapper, including already-imported aliases."""
        import sys
        from apps.proxy.live_proxy import url_utils
        with TooManyStreams._lock:
            if getattr(Channel, '_tms_hook_state', None):
                return
            TooManyStreams.check_requirements_met()
            owner = TooManyStreams
            observed = ContextVar('tms_native_selection', default=None)
            original = Channel.get_stream
            generate = url_utils.generate_stream_url
            state = {'enabled': True, 'patches': [], 'owner': owner}

            @wraps(original)
            def get_stream(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                observations = observed.get()
                if observations is not None:
                    observations.append((self, result))
                return result

            @wraps(generate)
            def generate_stream_url(*args, **kwargs):
                observations = []
                token = observed.set(observations)
                try:
                    result = generate(*args, **kwargs)
                    # Exact, pinned error contract only; never mask exceptions,
                    # URL-transform failures, inactive accounts or authorization.
                    if (not state['enabled'] or len(observations) != 1
                            or len(result) != 7 or result[0] is not None
                            or result[4] is not False
                            or result[5] != owner.CAPACITY_ERROR):
                        return result
                    channel, selection = observations[0]
                    if selection != (None, None, owner.CAPACITY_ERROR, False):
                        return result
                    server = owner._server
                    if server is None:
                        # Multiworker peer: verify the shared listener is alive.
                        if not owner._peer_healthy(*owner._bind_address()):
                            return result
                    elif not server.is_running:
                        return result
                    try:
                        profile = channel.get_stream_profile()
                        # Loopback URL must never be redirected to a viewer. Do
                        # not reinterpret requester-specific redirect restrictions.
                        if profile is None or profile.is_redirect():
                            return result
                        if not owner._confirmed_capacity(channel):
                            return result
                        warning = owner.get_or_create_stream()
                        if RedisClient.get_client().get(f'stream_profile:{warning.id}') is not None:
                            return result
                        override = kwargs.get('override_stream_profile_id')
                        if override is None and len(args) > 3:
                            override = args[3]
                        transcode = False if override is not None else not profile.is_proxy()
                        profile_id = override if override is not None else profile.id
                    except Exception as exc:
                        # Native exceptions may contain credential-bearing URLs.
                        logger.warning('Could not prepare capacity warning (%s); retaining native failure', type(exc).__name__)
                        return result
                    if not state['enabled']:
                        return result
                    if server is not None and not server.is_running:
                        return result
                    owner.REFRESH_SIGNAL.set()
                    return warning.url, None, transcode, profile_id, False, None, warning.id
                finally:
                    observed.reset(token)

            # Existing from-import aliases otherwise continue calling the native
            # resolver. Do not import views here (it imports this module's host).
            targets = [(Channel, 'get_stream', original, get_stream)]
            for name, mod in list(sys.modules.items()):
                if name == 'apps.proxy.live_proxy' or name.startswith('apps.proxy.live_proxy.'):
                    if mod is not None and getattr(mod, 'generate_stream_url', None) is generate:
                        targets.append((mod, 'generate_stream_url', generate, generate_stream_url))
            for obj, name, before, after in targets:
                setattr(obj, name, after)
                state['patches'].append((obj, name, before, after))
            Channel._tms_hook_state = state

    @staticmethod
    def uninstall_get_stream_override():
        with TooManyStreams._lock:
            import sys
            state = getattr(Channel, '_tms_hook_state', None)
            if not state:
                return
            state['enabled'] = False
            # Restore aliases imported after installation as well.
            for modname, mod in list(sys.modules.items()):
                if modname.startswith('apps.proxy.live_proxy.') and mod is not None:
                    for _, name, before, after in state['patches']:
                        if name == 'generate_stream_url' and getattr(mod, name, None) is after:
                            setattr(mod, name, before)
            for obj, name, before, after in reversed(state['patches']):
                if getattr(obj, name, None) is after:
                    setattr(obj, name, before)
            del Channel._tms_hook_state

    @staticmethod
    def start(image_path=None, host='127.0.0.1', port=1337):
        with TooManyStreams._lock:
            TooManyStreams.check_requirements_met()
            state = getattr(Channel, '_tms_hook_state', None)
            if state:
                TooManyStreams._server = state['owner']._server
                return
            if TooManyStreams._server is not None:
                return
            # Legacy associations would be selected as normal Custom streams and
            # allocate fake upstream slots. Refuse rather than changing channels.
            if ChannelStream.objects.filter(
                stream__name=TooManyStreams.STREAM_NAME,
                stream__is_custom=True,
            ).exists():
                raise RuntimeError('Remove legacy TooManyStreams channel associations before enabling')
            server = StreamServer(host, port, image_path, TooManyStreams.REFRESH_SIGNAL)
            try:
                server.start()  # Bind synchronously; no hooks on bind/FFmpeg failure.
                TooManyStreams._server = server
                TooManyStreams.install_get_stream_override()
            except OSError as exc:
                # Multiworker hosts (uwsgi/celery/daphne) run discovery in every
                # process. The first worker owns the shared loopback port; the
                # rest attach hooks and share its stream URL. Fail only if no
                # healthy instance answers.
                server.stop()
                if exc.errno not in (48, 98) or not TooManyStreams._peer_healthy(host, port):
                    TooManyStreams._server = None
                    raise
                logger.info('Port %s already served by another TooManyStreams worker; attaching hooks without a local listener', port)
                TooManyStreams._server = None  # Never stop a peer's server.
                TooManyStreams.install_get_stream_override()
            except Exception:
                TooManyStreams._server = None
                server.stop()
                raise

    @staticmethod
    def _bind_address():
        from .TooManyStreamsConfig import TooManyStreamsConfig
        return TooManyStreamsConfig.get_host_and_port()

    @staticmethod
    def _peer_healthy(host, port, timeout=3):
        import urllib.request
        try:
            with urllib.request.urlopen(f'http://{host}:{port}/stream.ts', timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def stop():
        with TooManyStreams._lock:
            state = getattr(Channel, '_tms_hook_state', None)
            owner = state['owner'] if state else TooManyStreams
            TooManyStreams.uninstall_get_stream_override()
            server, owner._server = owner._server, None
            TooManyStreams._server = None
            if server is not None:
                server.stop()

    @staticmethod
    def apply_to_all_channels():
        # Retain the old action id, but no global mutation is needed anymore.
        TooManyStreams.get_or_create_stream()
        logger.info('Capacity fallback is automatic; no channel associations added')

    @staticmethod
    def remove_from_all_channels():
        # Explicit migration action only. Never stop healthy channels or alter
        # provider streams, priorities, permissions or native fallback order.
        ChannelStream.objects.filter(
            stream__name=TooManyStreams.STREAM_NAME,
            stream__is_custom=True,
        ).delete()
