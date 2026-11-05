"""Offline contract regressions. No Dispatcharr, database or provider is contacted.

TMS_NATIVE_ROOT optionally exercises the pinned host's actual Channel.get_stream
and generate_stream_url function bodies with synthetic ORM/Redis dependencies.
Native AGPL source is not copied into this repository.
"""
import ast
import importlib
import logging
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
FULL = 'All active M3U profiles have reached maximum connection limits'


class Rows(list):
    def all(self):
        return self

    def exists(self):
        return bool(self)

    def filter(self, **kw):
        return Rows(x for x in self if all(getattr(x, k) == v for k, v in kw.items()))

    def order_by(self, *_):
        return self


class Redis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def hget(self, *_):
        return None

    def incr(self, key):
        self.data[key] = int(self.get(key) or 0) + 1
        return self.data[key]

    def expire(self, *_):
        pass


def module(name, **attrs):
    obj = types.ModuleType(name)
    obj.__dict__.update(attrs)
    return obj


def native_body(path, name, cls=None):
    text = path.read_text()
    tree = ast.parse(text)
    nodes = tree.body if cls is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body
    node = next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(text, node)


class Compatibility(unittest.TestCase):
    def setUp(self):
        self.redis = Redis()
        self.calls = []
        self.response = (10, 20, None, True)
        self.stream = types.SimpleNamespace(id=900, url='http://127.0.0.1:1337/stream.ts')
        outer = self

        class Channel:
            def __init__(self, ident=1, streams=None, redirect=False):
                self.id = ident
                self.uuid = str(ident)
                self.streams = streams or Rows()
                self.redirect = redirect

            def get_stream(self, *args, **kwargs):
                outer.calls.append((self, args, kwargs))
                if isinstance(outer.response, Exception):
                    raise outer.response
                return outer.response

            def get_stream_profile(self):
                return types.SimpleNamespace(id=77, is_redirect=lambda: self.redirect, is_proxy=lambda: True)

            def _stream_assignment_is_reusable(self, *_):
                return True

            def _pick_channel_to_preempt(self, **_):
                return None

        self.Channel = Channel
        self.account = types.SimpleNamespace(id=1, is_active=True, get_user_agent_string=lambda: 'test')
        self.profile = types.SimpleNamespace(id=20, is_active=True, is_default=True, max_streams=1, name='test', m3u_account=self.account)
        self.account.profiles = Rows([self.profile])
        self.assigned = types.SimpleNamespace(id=10, name='native', m3u_account=self.account, m3u_account_id=1)
        self.channel = Channel(streams=Rows([self.assigned]))
        self.redis.set('profile_connections:20', 1)
        streams = Mock()
        streams.get.return_value = self.assigned
        streams.get_or_create.return_value = (self.stream, False)
        streams.filter.return_value.first.return_value = self.stream
        self.Stream = type('Stream', (), {'objects': streams})
        self.url_calls = []

        def generate(channel_id, user=None, allowed_m3u_profiles=None, override_stream_profile_id=None):
            self.url_calls.append((channel_id, user, allowed_m3u_profiles, override_stream_profile_id))
            sid, pid, error, reserved = self.channel.get_stream(user, allowed_m3u_profiles)
            if error:
                return None, None, False, None, False, error, None
            return 'native-url', 'native-ua', False, 77, reserved, None, sid

        self.url_module = module('apps.proxy.live_proxy.url_utils', generate_stream_url=generate)
        self.views = module('apps.proxy.live_proxy.views', generate_stream_url=generate)
        self.models = module('apps.channels.models', Channel=Channel, Stream=self.Stream, ChannelStream=Mock())
        self.models.ChannelStream.objects.filter.return_value.exists.return_value = False
        modules = {
            'apps.channels.models': self.models,
            'apps.proxy.live_proxy.url_utils': self.url_module,
            'apps.proxy.live_proxy.views': self.views,
            'apps.proxy.live_proxy.server': module('server', ProxyServer=Mock()),
            'apps.proxy.live_proxy.services.channel_service': module('service', ChannelService=Mock()),
            'apps.proxy.live_proxy.constants': module('constants', ChannelMetadataField=types.SimpleNamespace(M3U_PROFILE='m3u_profile')),
            'apps.proxy.live_proxy.redis_keys': module('keys', RedisKeys=types.SimpleNamespace(channel_metadata=lambda c: f'live:channel:{c}:metadata')),
            'apps.m3u.connection_pool': module('pool', get_profile_connection_count=lambda p, r: int(r.get(f'profile_connections:{p.id}') or 0), get_credential_connection_count=lambda p, r: 0),
            'apps.m3u.utils': module('m3u.utils', get_allowed_m3u_profiles=lambda _: {}),
            'core.utils': module('core.utils', RedisClient=types.SimpleNamespace(get_client=lambda: self.redis)),
            'version': module('version', __version__='0.33.0'),
            'tms_test.src.StreamServer': module('server', StreamServer=Mock()),
        }
        # Parent packages allow imports without executing Dispatcharr setup.
        for name in list(modules):
            parts = name.split('.')
            for i in range(1, len(parts)):
                key = '.'.join(parts[:i])
                modules.setdefault(key, module(key, __path__=[]))
        modules['tms_test'] = module('tms_test', __path__=[str(ROOT)])
        modules['tms_test.src'] = module('tms_test.src', __path__=[str(ROOT / 'src')])
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        for name in ['tms_test.src.TooManyStreams', 'tms_test.src.TooManyStreamsConfig', 'tms_test.src.schemas']:
            sys.modules.pop(name, None)
        self.mod = importlib.import_module('tms_test.src.TooManyStreams')
        self.TMS = self.mod.TooManyStreams
        self.config = self.mod.TooManyStreamsConfig
        self.config.get_stream_url = lambda: self.stream.url
        self.TMS.get_or_create_stream = Mock(return_value=self.stream)
        self.TMS._server = Mock(is_running=True)
        self.addCleanup(lambda: getattr(self.TMS, 'uninstall_get_stream_override', lambda: None)())
        self.native = Channel.get_stream
        self.native_url = generate

    def install(self):
        self.TMS.install_get_stream_override()

    def test_success_exact_identity_and_kwargs(self):
        self.install()
        marker = object()
        self.assertIs(self.channel.get_stream(marker, allowed_m3u_profiles={}, future=marker), self.response)
        self.assertEqual(self.calls[0][1:], ((marker,), {'allowed_m3u_profiles': {}, 'future': marker}))

    def test_first_request_capacity_warning_has_no_slot_or_mapping(self):
        self.response = (None, None, FULL, False)
        before = dict(self.redis.data)
        self.install()
        self.assertEqual(self.views.generate_stream_url('1'), (self.stream.url, None, False, 77, False, None, 900))
        self.assertEqual(self.redis.data, before)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.models.ChannelStream.mock_calls, [])

    def test_all_noncapacity_failures_and_exceptions_unchanged(self):
        self.install()
        for error in ['No streams assigned to channel', 'No active profiles found for any assigned stream', 'No compatible active profile found for any assigned stream', 'Redis unavailable']:
            self.response = (None, None, error, False)
            self.assertIs(self.channel.get_stream(), self.response)
            self.assertEqual(self.views.generate_stream_url('1')[5], error)
        self.response = RuntimeError('native failure')
        with self.assertRaisesRegex(RuntimeError, 'native failure'):
            self.channel.get_stream()

    def test_fail_closed_when_capacity_recovers(self):
        self.response = (None, None, FULL, False)
        self.redis.set('profile_connections:20', 0)
        self.install()
        self.assertEqual(self.views.generate_stream_url('1')[5], FULL)

    def test_counter_errors_preserve_failure_without_logging_urls(self):
        self.response = (None, None, FULL, False)
        setattr(self.mod, 'get_profile_connection_count', Mock(side_effect=ValueError('https://provider.invalid/live/user/private-password/1.ts')))
        self.install()
        with self.assertLogs('plugins.too_many_streams.TooManyStreams', level='WARNING') as logs:
            self.assertEqual(self.views.generate_stream_url('1')[5], FULL)
        rendered = str(logs.output)
        self.assertIn('ValueError', rendered)
        self.assertNotIn('private-password', rendered)
        self.assertNotIn('provider.invalid', rendered)

    def test_inactive_account_missing_default_and_redirect_not_bypassed(self):
        self.response = (None, None, FULL, False)
        self.install()
        for obj, key in [(self.account, 'is_active'), (self.profile, 'is_active'), (self.profile, 'is_default')]:
            setattr(obj, key, False)
            self.assertEqual(self.views.generate_stream_url('1')[5], FULL)
            setattr(obj, key, True)
        self.channel.redirect = True
        self.assertEqual(self.views.generate_stream_url('1', object(), {})[5], FULL)

    def test_all_profiles_must_be_full(self):
        self.response = (None, None, FULL, False)
        self.account.profiles.append(types.SimpleNamespace(id=21, is_default=False, is_active=True, max_streams=2))
        self.install()
        self.assertEqual(self.views.generate_stream_url('1')[5], FULL)
        self.redis.set('profile_connections:21', 2)
        self.assertEqual(self.views.generate_stream_url('1')[0], self.stream.url)

    def test_native_url_success_identity_and_argument_forwarding(self):
        sentinel = ('native', object(), False, 19, False, None, 10)
        self.url_module.generate_stream_url = Mock(return_value=sentinel)
        self.views.generate_stream_url = self.url_module.generate_stream_url
        self.install()
        marker = object()
        self.assertIs(self.views.generate_stream_url('1', user=marker, allowed_m3u_profiles={}, override_stream_profile_id=77), sentinel)
        self.url_module.generate_stream_url.__wrapped__.assert_called_once_with('1', user=marker, allowed_m3u_profiles={}, override_stream_profile_id=77)

    def test_hooks_idempotent_removed_and_foreign_hook_preserved(self):
        self.install()
        first = self.Channel.get_stream
        self.install()
        self.assertIs(self.Channel.get_stream, first)
        self.TMS.uninstall_get_stream_override()
        self.assertIs(self.Channel.get_stream, self.native)
        self.assertIs(self.views.generate_stream_url, self.native_url)
        self.install()
        foreign = lambda *_: None
        self.Channel.get_stream = foreign
        self.TMS.uninstall_get_stream_override()
        self.assertIs(self.Channel.get_stream, foreign)

    def test_unsupported_version_fails_before_patch(self):
        sys.modules['version'].__version__ = '0.30.0'
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Dispatcharr'):
            self.install()
        self.assertIs(self.Channel.get_stream, self.native)

    def test_server_down_preserves_error(self):
        self.response = (None, None, FULL, False)
        self.TMS._server.is_running = False
        self.install()
        self.assertEqual(self.views.generate_stream_url('1')[5], FULL)

    def test_multiworker_peer_attaches_hooks_without_listener(self):
        # Simulate a second process: port owned by a healthy peer, no local server.
        import errno
        self.install()
        self.assertTrue(hasattr(self.Channel.get_stream, '__wrapped__'))
        self.TMS.uninstall_get_stream_override()
        owner_server = self.TMS._server
        self.TMS._server = None
        server_mod = sys.modules['tms_test.src.StreamServer']
        busy = OSError(errno.EADDRINUSE, 'Address already in use')
        failing = Mock()
        failing.start.side_effect = busy
        server_mod.StreamServer.return_value = failing
        self.addCleanup(setattr, server_mod.StreamServer, 'return_value', server_mod.StreamServer.return_value)
        healthy = patch.object(self.TMS, '_peer_healthy', return_value=True)
        healthy.start()
        self.addCleanup(healthy.stop)
        self.TMS.start(None, '127.0.0.1', 1337)
        self.assertIsNone(self.TMS._server, 'peer must not own/stop a listener')
        self.assertTrue(hasattr(self.Channel.get_stream, '__wrapped__'), 'peer hooks installed')
        # Warning path still fires through the shared listener.
        self.response = (None, None, FULL, False)
        result = self.views.generate_stream_url('1')
        self.assertEqual(result[6], self.stream.id, result)
        self.assertIsNone(result[1], 'warning must not claim a provider profile')
        self.TMS.stop()  # peer stop must not kill the owner's listener
        self.assertTrue(owner_server.is_running)

    def test_multiworker_peer_fails_when_listener_unhealthy(self):
        import errno
        if hasattr(self.Channel.get_stream, '__wrapped__'):
            self.TMS.uninstall_get_stream_override()
        self.TMS._server = None
        server_mod = sys.modules['tms_test.src.StreamServer']
        busy = OSError(errno.EADDRINUSE, 'Address already in use')
        failing = Mock()
        failing.start.side_effect = busy
        server_mod.StreamServer.return_value = failing
        self.addCleanup(setattr, server_mod.StreamServer, 'return_value', server_mod.StreamServer.return_value)
        with patch.object(self.TMS, '_peer_healthy', return_value=False):
            with self.assertRaises(OSError):
                self.TMS.start(None, '127.0.0.1', 1337)
        self.assertFalse(hasattr(self.Channel.get_stream, '__wrapped__'))

    def test_stale_native_allocation_identities_never_attached_to_warning(self):
        self.response = (None, None, FULL, False)
        self.install()
        for key in ['channel_stream:1', 'stream_profile:900']:
            self.redis.set(key, 20)
            before = dict(self.redis.data)
            self.assertEqual(self.views.generate_stream_url('1')[5], FULL)
            self.assertEqual(self.redis.data, before)
            self.redis.data.pop(key)
        self.redis.hget = lambda *_: b'20'
        self.assertEqual(self.views.generate_stream_url('1')[5], FULL)

    def test_plugin_loader_stop_contract_and_reinitialize(self):
        with patch.object(self.config, 'get_plugin_persistent_config', return_value={}), patch.object(self.config, 'get_config', return_value=types.SimpleNamespace(tms_log_level='INFO', tms_image_path=None)), patch.object(self.TMS, 'start') as start, patch.object(self.TMS, 'stop') as stop:
            sys.modules.pop('tms_test.plugin', None)
            plugin = importlib.import_module('tms_test.plugin').Plugin()
            plugin.initialize()
            start.assert_called_once()
            plugin.stop({'reason': 'disabled'})
            stop.assert_called_once_with()
            self.assertFalse(plugin.initialized)
            plugin.initialize()
            self.assertEqual(start.call_count, 2)

    def test_shared_credential_pool_capacity_is_recognized(self):
        self.response = (None, None, FULL, False)
        self.redis.set('profile_connections:20', 0)
        self.mod.get_credential_connection_count = lambda *_: 1
        self.install()
        self.assertEqual(self.views.generate_stream_url('1')[0], self.stream.url)
        self.assertEqual(self.redis.get('profile_connections:20'), 0)

    def test_unlimited_profile_and_unrelated_url_errors_not_masked(self):
        self.response = (None, None, FULL, False)
        self.profile.max_streams = 0
        self.install()
        self.assertEqual(self.views.generate_stream_url('1')[5], FULL)
        self.TMS.uninstall_get_stream_override()
        failure = (None, None, False, None, False, FULL, None)
        self.url_module.generate_stream_url = self.views.generate_stream_url = Mock(return_value=failure)
        self.install()
        self.assertIs(self.views.generate_stream_url('1'), failure)

    def test_url_exception_propagates_and_observation_context_is_reset(self):
        native = self.url_module.generate_stream_url
        self.url_module.generate_stream_url = self.views.generate_stream_url = Mock(side_effect=ValueError('resolver broke'))
        self.install()
        with self.assertRaisesRegex(ValueError, 'resolver broke'):
            self.views.generate_stream_url('1')
        self.assertIs(self.channel.get_stream(), self.response)
        self.TMS.uninstall_get_stream_override()
        self.url_module.generate_stream_url = self.views.generate_stream_url = native

    def test_late_import_alias_is_restored(self):
        self.install()
        late = module('late', generate_stream_url=self.url_module.generate_stream_url)
        with patch.dict(sys.modules, {'apps.proxy.live_proxy.late': late}):
            self.TMS.uninstall_get_stream_override()
            self.assertIs(late.generate_stream_url, self.native_url)

    def test_warning_output_profile_override_is_preserved(self):
        self.response = (None, None, FULL, False)
        self.install()
        self.assertEqual(self.views.generate_stream_url('1', None, None, 123)[2:5], (False, 123, False))
        self.assertEqual(self.views.generate_stream_url('1', override_stream_profile_id=321)[2:5], (False, 321, False))

    def test_start_failure_never_installs_hook_and_stop_restores_resources(self):
        self.TMS._server = None
        self.models.ChannelStream.objects.filter.return_value.exists.return_value = False
        server = self.mod.StreamServer.return_value
        server.start.side_effect = OSError('port in use')
        with self.assertRaisesRegex(OSError, 'port in use'):
            self.TMS.start()
        self.assertIs(self.Channel.get_stream, self.native)
        server.stop.assert_called_once_with()
        server.start.side_effect = None
        self.TMS.start()
        self.TMS.start()
        self.assertEqual(server.start.call_count, 2)  # one failed, one successful
        self.TMS.stop()
        self.TMS.stop()
        self.assertIs(self.Channel.get_stream, self.native)
        self.assertIsNone(self.TMS._server)
        self.assertEqual(server.stop.call_count, 2)

    def test_legacy_channel_associations_fail_closed_and_actions_do_not_stop_channels(self):
        self.TMS._server = None
        self.models.ChannelStream.objects.filter.return_value.exists.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'legacy'):
            self.TMS.start()
        self.TMS.apply_to_all_channels()
        self.TMS.get_or_create_stream.assert_called_once_with()
        self.TMS.remove_from_all_channels()
        self.models.ChannelStream.objects.filter.return_value.delete.assert_called_once_with()
        self.models.ChannelStream.objects.filter.assert_any_call(
            stream__name='TooManyStreams', stream__is_custom=True,
        )  # Match legacy localhost/0.0.0.0/old-port URLs too.
        self.assertIs(self.Channel.get_stream, self.native)

    @unittest.skipUnless(os.environ.get('TMS_NATIVE_ROOT'), 'set TMS_NATIVE_ROOT to pinned Dispatcharr source')
    def test_pinned_native_selection_reuse_restrictions_and_url_boundary(self):
        root = Path(os.environ['TMS_NATIVE_ROOT'])
        namespace = dict(vars(self.models), RedisClient=sys.modules['core.utils'].RedisClient, logger=logging.getLogger('native'), pool_has_capacity_for_profile=lambda p, r: False)

        def reserve(profile, redis):
            count = int(redis.get(f'profile_connections:{profile.id}') or 0)
            if count >= profile.max_streams:
                return False, count, 'profile_full'
            redis.set(f'profile_connections:{profile.id}', count + 1)
            return True, count + 1, None

        namespace['reserve_profile_slot'] = reserve
        exec(native_body(root / 'apps/channels/models.py', 'get_stream', 'Channel'), namespace)
        self.Channel.get_stream = namespace['get_stream']
        self.native = self.Channel.get_stream
        # Execute the pinned real URL resolver, with only external dependencies stubbed.
        url_ns = dict(namespace, Optional=__import__('typing').Optional, Tuple=__import__('typing').Tuple,
                      get_stream_object=lambda _: self.channel, close_old_connections=lambda: None,
                      parse_preview_worker_id=lambda _: (None, None),
                      _resolve_live_stream_url=lambda *_: 'native-url',
                      M3UAccountProfile=types.SimpleNamespace(objects=Mock()))
        url_ns['M3UAccountProfile'].objects.select_related.return_value.get.return_value = self.profile
        exec(native_body(root / 'apps/proxy/live_proxy/url_utils.py', 'generate_stream_url'), url_ns)
        self.native_url = url_ns['generate_stream_url']
        self.url_module.generate_stream_url = self.views.generate_stream_url = self.native_url
        self.install()
        self.redis.set('profile_connections:20', 0)
        one = self.views.generate_stream_url('1')
        self.assertEqual(one, ('native-url', 'test', False, 77, True, None, 10))
        self.assertEqual(self.redis.get('profile_connections:20'), 1)
        # A second viewer reuses the SAME channel without a second reservation.
        two = self.views.generate_stream_url('1')
        self.assertEqual(two[0], 'native-url')
        self.assertFalse(two[4])
        self.assertEqual(self.redis.get('profile_connections:20'), 1)
        # Another channel cannot consume that slot; its first request gets warning.
        self.channel = self.Channel(ident=2, streams=Rows([self.assigned]))
        before = dict(self.redis.data)
        self.assertEqual(self.views.generate_stream_url('2')[0], self.stream.url)
        self.assertEqual(self.redis.data, before)
        # Even an explicit native teardown of the warning channel finds no
        # allocation keys and cannot decrement the healthy channel's slot.
        namespace.update(
            RedisKeys=sys.modules['apps.proxy.live_proxy.redis_keys'].RedisKeys,
            ChannelMetadataField=types.SimpleNamespace(STREAM_ID='stream_id', M3U_PROFILE='m3u_profile'),
            redis_getdel=lambda redis, key: redis.data.pop(key, None),
        )
        exec(native_body(root / 'apps/channels/models.py', 'release_stream', 'Channel'), namespace)
        self.assertFalse(namespace['release_stream'](self.channel))
        self.assertEqual(self.redis.data, before)
        self.account.is_active = False
        self.assertIsNone(self.views.generate_stream_url('2')[0])
        self.account.is_active = True
        self.channel.redirect = True
        self.assertIsNone(self.views.generate_stream_url('2', object(), {})[0])
        # Native stream/profile ordering wins over warning: try the next
        # assigned upstream stream when the first provider is full.
        next_profile = types.SimpleNamespace(id=21, is_active=True, is_default=True, max_streams=1, name='next')
        next_account = types.SimpleNamespace(id=2, is_active=True, profiles=Rows([next_profile]))
        next_stream = types.SimpleNamespace(id=11, m3u_account=next_account)
        next_channel = self.Channel(3, streams=Rows([self.assigned, next_stream]))
        self.assertEqual(next_channel.get_stream(), (11, 21, None, True))
        self.assertEqual(self.redis.get('profile_connections:21'), 1)
        self.assertEqual(self.redis.get('channel_stream:3'), 11)


class ServerSafety(unittest.TestCase):
    def setUp(self):
        self.spawn = Mock()
        modules = {
            'tms_server': module('tms_server', __path__=[str(ROOT)]),
            'tms_server.src': module('tms_server.src', __path__=[str(ROOT / 'src')]),
            'tms_server.src.PillowImageGen': module('image', PillowImageGen=Mock()),
            'tms_server.src.TooManyStreamsConfig': module('config', TooManyStreamsConfig=types.SimpleNamespace(get_config=lambda: types.SimpleNamespace(video_encoder='libx264'))),
            'apps.proxy.live_proxy.utils': module('utils', posix_spawn_proc=self.spawn),
            'django.db': module('db', close_old_connections=Mock()),
        }
        for name in list(modules):
            parts = name.split('.')
            for i in range(1, len(parts)):
                key = '.'.join(parts[:i])
                modules.setdefault(key, module(key, __path__=[]))
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        sys.modules.pop('tms_server.src.StreamServer', None)
        self.mod = importlib.import_module('tms_server.src.StreamServer')
        self.server = self.mod.StreamServer('127.0.0.1', 0, '/no-such-static-image')
        self.addCleanup(self.server.stop)

    def process(self):
        import io
        proc = Mock()
        proc.stdin, proc.stdout, proc.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    def test_ffmpeg_recovery_does_not_reenter_nonreentrant_lock(self):
        proc = self.process()
        def spawn(_):
            self.server._stopping.set()
            return proc
        self.spawn.side_effect = spawn
        thread = threading.Thread(target=self.server._broadcaster_loop, daemon=True)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), 'Recovery deadlocked on nested process_lock')
        self.assertIs(self.server.process, proc)

    def test_concurrent_restarts_reap_all_old_processes_and_stop_is_idempotent(self):
        processes = []
        def spawn(_):
            proc = self.process()
            processes.append(proc)
            return proc
        self.spawn.side_effect = spawn
        threads = [threading.Thread(target=self.server._start_ffmpeg) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(processes), 8)
        for proc in processes[:-1]:
            proc.terminate.assert_called_once_with()
            proc.wait.assert_called_once_with(timeout=2)
            self.assertTrue(proc.stdout.closed)
        self.server.stop()
        self.server.stop()
        processes[-1].terminate.assert_called_once_with()
        self.assertIsNone(self.server.process)

    def test_kill_fallback_waits_and_closes_pipes(self):
        import subprocess
        proc = self.process()
        proc.wait.side_effect = [subprocess.TimeoutExpired('ffmpeg', 2), 0]
        self.server._close_process(proc)
        proc.kill.assert_called_once_with()
        self.assertEqual(proc.wait.call_count, 2)
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)

    def test_missing_static_image_closes_listener_without_workers(self):
        self.server.ffmpeg_bin = '/usr/bin/ffmpeg'
        with self.assertRaisesRegex(RuntimeError, 'Static warning image'):
            self.server.start()
        self.assertIsNone(self.server._httpd)
        self.assertEqual(self.server._threads, [])
        self.spawn.assert_not_called()
        self.mod.PillowImageGen.assert_not_called()

    def test_idle_and_partial_http_clients_cannot_hang_stop(self):
        import socket
        import tempfile
        import time
        proc = self.process()
        failures = []
        sockets = []
        with tempfile.NamedTemporaryFile() as image:
            self.server.image_path = image.name
            self.server.ffmpeg_bin = '/test/ffmpeg'
            with patch.object(self.server, '_start_ffmpeg', side_effect=lambda: setattr(self.server, 'process', proc)), patch.object(self.server, '_broadcaster_loop', return_value=None):
                self.server.start()
                port = self.server._httpd.server_address[1]
                sockets.append(socket.create_connection(('127.0.0.1', port), timeout=1))
                sockets.append(socket.create_connection(('127.0.0.1', port), timeout=1))
                sockets[1].sendall(b'GET /stream.ts HTTP/1.1\r\nHost: unfinished')
                deadline = time.monotonic() + 2
                while len(self.server._httpd._threads) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(self.server._httpd._threads), 2)
                def stop():
                    try:
                        self.server.stop()
                    except Exception as exc:
                        failures.append(type(exc).__name__)
                worker = threading.Thread(target=stop, daemon=True)
                worker.start()
                worker.join(timeout=4)
                stopped_with_clients_open = not worker.is_alive()
                for client in sockets:
                    client.close()
                worker.join(timeout=4)
                self.assertFalse(worker.is_alive(), 'Cleanup thread leaked')
                self.assertTrue(stopped_with_clients_open, 'Idle/incomplete HTTP request hung shutdown')
                self.assertFalse(failures, failures)
                proc.terminate.assert_called_once_with()

    def test_encoder_is_paced_and_yuv420p(self):
        command = self.server._get_ffmpeg_cmd('/safe.jpg')
        self.assertIn('-re', command)
        self.assertEqual(command[command.index('-pix_fmt') + 1], 'yuv420p')


if __name__ == '__main__':
    unittest.main()
