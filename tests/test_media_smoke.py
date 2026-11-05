"""Real loopback HTTP + FFmpeg smoke test; no providers, DB, or real credentials."""
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
if os.environ.get('TMS_REQUIRE_MEDIA_TESTS') == '1' and not AVAILABLE:
    raise RuntimeError('FFmpeg and ffprobe are required for the media acceptance test')


@unittest.skipUnless(AVAILABLE, 'FFmpeg/ffprobe not installed')
class MediaSmokeTests(unittest.TestCase):
    def test_loopback_server_produces_decodable_video_and_stops(self):
        from PIL import Image

        namespace = '_tms_real_media'
        package = types.ModuleType(namespace)
        package.__path__ = [str(ROOT)]
        src = types.ModuleType(namespace + '.src')
        src.__path__ = [str(ROOT / 'src')]
        modules = {namespace: package, namespace + '.src': src}
        # Inject only the host process/DB adapters. FFmpeg, sockets, encoded
        # bytes, decoding and cleanup below are real; no provider is contacted.
        for name in ('apps', 'apps.proxy', 'apps.proxy.live_proxy'):
            host_module = types.ModuleType(name)
            host_module.__path__ = []
            modules[name] = host_module
        native_utils = types.ModuleType('apps.proxy.live_proxy.utils')
        setattr(native_utils, 'posix_spawn_proc', lambda cmd: subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
        ))
        modules[native_utils.__name__] = native_utils
        django = types.ModuleType('django')
        django.__path__ = []
        db = types.ModuleType('django.db')
        setattr(db, 'close_old_connections', lambda: None)
        modules.update({'django': django, 'django.db': db})
        with patch.dict(sys.modules, modules):
            config_type = importlib.import_module(namespace + '.src.schemas').PluginConfig
            config_module = types.ModuleType(namespace + '.src.TooManyStreamsConfig')
            setattr(config_module, 'TooManyStreamsConfig', type('Config', (), {'get_config': staticmethod(config_type)}))
            image_module = types.ModuleType(namespace + '.src.PillowImageGen')

            class ExistingImage:
                def __init__(self, **kwargs):
                    pass

                def generate(self, **kwargs):
                    return False

                def get_active_streams(self):
                    return []

            setattr(image_module, 'PillowImageGen', ExistingImage)
            with patch.dict(sys.modules, {config_module.__name__: config_module, image_module.__name__: image_module}):
                server_type = importlib.import_module(namespace + '.src.StreamServer').StreamServer
                with tempfile.TemporaryDirectory() as tmp:
                    image = Path(tmp) / 'warning.jpg'
                    Image.new('RGB', (640, 360), (20, 35, 55)).save(image)
                    with socket.socket() as sock:
                        sock.bind(('127.0.0.1', 0))
                        port = sock.getsockname()[1]
                    server = server_type('127.0.0.1', port, image_path=str(image))
                    failures = []

                    def serve():
                        try:
                            server.start()
                        except Exception as exc:
                            failures.append(type(exc).__name__)

                    worker = threading.Thread(target=serve, daemon=True)
                    worker.start()
                    process = None
                    try:
                        deadline = time.monotonic() + 12
                        while True:
                            try:
                                response = urllib.request.urlopen(f'http://127.0.0.1:{port}/stream.ts', timeout=12)
                                break
                            except urllib.error.URLError:
                                if time.monotonic() >= deadline:
                                    self.fail(f'Warning listener did not start: {failures}')
                                time.sleep(0.05)
                        with response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.headers.get_content_type(), 'video/mp2t')
                            sample = response.read(65536)
                        process = server.process
                        self.assertGreater(len(sample), 188 * 5)
                        self.assertTrue(any(all(sample[i + 188*j] == 0x47 for j in range(5)) for i in range(188)))
                        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name,codec_type,pix_fmt', '-of', 'json', '-i', 'pipe:0'], input=sample, capture_output=True, timeout=15)
                        self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors='replace'))
                        codecs = json.loads(probe.stdout)['streams']
                        self.assertTrue(any(s.get('codec_name') == 'h264' and s.get('pix_fmt') == 'yuv420p' for s in codecs), codecs)
                        self.assertTrue(any(s.get('codec_name') == 'aac' for s in codecs), codecs)
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            urllib.request.urlopen(f'http://127.0.0.1:{port}/not-a-stream', timeout=3)
                        self.assertEqual(denied.exception.code, 404)
                        denied.exception.close()
                    finally:
                        server.stop()
                        worker.join(timeout=8)
                    self.assertFalse(worker.is_alive(), 'HTTP listener thread leaked')
                    self.assertFalse(failures, failures)
                    if process is not None:
                        self.assertIsNotNone(process.poll(), 'FFmpeg child process leaked')


if __name__ == '__main__':
    unittest.main()
