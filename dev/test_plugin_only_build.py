"""Portable build coverage without Blender or private character packages.

Run: python dev/test_plugin_only_build.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_release as br


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PluginOnlyBuildTests(unittest.TestCase):
    def test_api_never_reads_example_assets(self):
        package = mock.Mock()
        audit = mock.Mock()
        package.exists.side_effect = AssertionError('example package must not be inspected')
        package.read_bytes.side_effect = AssertionError('example package must not be read')
        audit.read_text.side_effect = AssertionError('example audit must not be read')
        with tempfile.TemporaryDirectory(prefix='cmb-plugin-only-api-') as directory:
            with mock.patch.object(br, 'PACKAGE', package), mock.patch.object(br, 'PACKAGE_AUDIT', audit):
                summary = br.build(Path(directory), plugin_only=True)
            self.assertTrue({'package', 'package_sha256', 'package_audit'}.isdisjoint(summary))
            self.assertEqual(package.mock_calls, [])
            self.assertEqual(audit.mock_calls, [])
            for record in summary['variants'].values():
                path = Path(record['addon'])
                self.assertEqual(sha(path), record['addon_sha256'])
                with zipfile.ZipFile(path) as archive:
                    self.assertIsNone(archive.testzip())

    def test_default_mode_keeps_example_metadata(self):
        with tempfile.TemporaryDirectory(prefix='cmb-default-build-') as directory:
            root = Path(directory)
            package = root / 'example.blend'
            package.write_bytes(b'example-hash-fixture')
            audit = root / 'audit.json'
            expected_audit = {'fixture': 'build metadata only; not a Blender runtime test'}
            audit.write_text(json.dumps(expected_audit), encoding='utf-8')
            with mock.patch.object(br, 'PACKAGE', package), mock.patch.object(br, 'PACKAGE_AUDIT', audit):
                summary = br.build(root / 'out')
            self.assertEqual(summary['package'], str(package))
            self.assertEqual(summary['package_sha256'], sha(package))
            self.assertEqual(summary['package_audit'], expected_audit)

    def test_clean_checkout_cli_and_original_zip_fingerprints(self):
        with tempfile.TemporaryDirectory(prefix='cmb-clean-checkout-') as directory:
            root = Path(directory)
            checkout = root / 'checkout'
            shutil.copytree(br.PLUGIN, checkout / 'character_material_bridge',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            dev = checkout / 'dev'
            dev.mkdir()
            for name in ('build_release.py', 'user_transform.py', 'README_user.md'):
                shutil.copyfile(br.ROOT / 'dev' / name, dev / name)
            self.assertFalse((checkout / 'packages').exists())
            self.assertFalse((checkout / 'dist').exists())
            self.assertFalse((dev / 'package_audit_v05pkg.json').exists())
            output = root / 'artifacts'
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
            command = [sys.executable, '-B', str(dev / 'build_release.py'), '--out', str(output)]
            result = subprocess.run(command + ['--plugin-only'], cwd=checkout, env=env,
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(json.loads((output / 'release.json').read_text(encoding='utf-8')), summary)
            self.assertTrue({'package', 'package_sha256', 'package_audit'}.isdisjoint(summary))
            self.assertEqual(len(list(output.iterdir())), 3)
            for record in summary['variants'].values():
                built = Path(record['addon'])
                self.assertEqual(sha(built), record['addon_sha256'])
                original = br.ROOT / 'dist' / built.name
                if original.is_file():
                    self.assertEqual(sha(built), sha(original), built.name)
            rejected = subprocess.run(command, cwd=checkout, env=env,
                                      capture_output=True, text=True, encoding='utf-8')
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('v0.5 package missing', rejected.stdout + rejected.stderr)
            init = checkout / 'character_material_bridge' / '__init__.py'
            original = init.read_bytes()
            outdated = original.replace(b"'blender': (5, 2, 0)", b"'blender': (4, 2, 0)", 1)
            self.assertNotEqual(outdated, original)
            init.write_bytes(outdated)
            rejected_version = subprocess.run(command + ['--plugin-only'], cwd=checkout, env=env,
                                              capture_output=True, text=True, encoding='utf-8')
            self.assertNotEqual(rejected_version.returncode, 0)
            self.assertIn('minimum Blender version', rejected_version.stdout + rejected_version.stderr)
            print('clean checkout CLI: PASS (no example package or audit; original ZIPs checked when present)')


if __name__ == '__main__':
    unittest.main(verbosity=2)
