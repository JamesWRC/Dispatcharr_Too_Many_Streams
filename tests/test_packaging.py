"""Distribution tests do not import the plugin or need a Dispatcharr server."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tms_build_plugin', ROOT / 'scripts/build_plugin.py')
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PackagingTests(unittest.TestCase):
    def test_source_matches_zip_and_no_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'plugin.zip'
            members = builder.build(output)
            self.assertEqual(len(members), len(set(members)))
            with ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                for name in archive.namelist():
                    self.assertTrue(name.startswith('too_many_streams/'))
                    relative = name.removeprefix('too_many_streams/')
                    self.assertEqual(archive.read(name), (ROOT / relative).read_bytes())
                    self.assertNotIn('__pycache__', name)
                    self.assertFalse(name.endswith(('.log', '.pyc', '.env')))
                for name in ('plugin.py', 'plugin.json', 'requirements.txt', 'LICENSE'):
                    self.assertIn('too_many_streams/' + name, archive.namelist())
                self.assertTrue(any(n.startswith('too_many_streams/img/') for n in members))

    def test_reproducible_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / 'one.zip', Path(tmp) / 'two.zip'
            builder.build(one)
            builder.build(two)
            self.assertEqual(hashlib.sha256(one.read_bytes()).digest(), hashlib.sha256(two.read_bytes()).digest())

    def test_metadata_version_matches_plugin(self):
        metadata = json.loads((ROOT / 'plugin.json').read_text())
        tree = ast.parse((ROOT / 'plugin.py').read_text())
        plugin = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Plugin')
        assignment = next(n for n in plugin.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'version' for t in n.targets))
        self.assertEqual(metadata['version'], ast.literal_eval(assignment.value))


if __name__ == '__main__':
    unittest.main()
