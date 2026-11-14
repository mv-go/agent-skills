import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_token_audit import event, root_rows, write_jsonl

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/codex-token-audit/scripts'


class DeliveryConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'codex'
        self.home.mkdir(mode=0o700)
        self.env = {**os.environ, 'CODEX_HOME': str(self.home)}
        self.config = self.home / 'codex-token-audit.json'

    def run_script(self, script, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)], env=self.env, text=True, capture_output=True)

    def configure(self, folder='folder_123', transport='connector'):
        return self.run_script('delivery_config.py', 'configure', '--drive-folder-id', folder, '--transport', transport)

    def test_configure_persists_private_setting_and_show_reads_it(self):
        self.assertEqual(self.configure().returncode, 0)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.config.read_text()), {'schema_version': 1, 'drive_folder_id': 'folder_123', 'transport': 'connector'})
        shown = self.run_script('delivery_config.py', 'show')
        self.assertEqual(shown.returncode, 0)
        self.assertIn('folder_123', shown.stdout)
        self.assertIn('connector', shown.stdout)

    def test_invalid_and_public_config_fail_without_echoing_contents(self):
        for content, mode in [('{secret', 0o600), (json.dumps({'schema_version': 1, 'drive_folder_id': 'secret/bad', 'transport': 'connector'}), 0o600), (json.dumps({'schema_version': 1, 'drive_folder_id': 'secret', 'transport': 'connector'}), 0o644)]:
            self.config.write_text(content)
            self.config.chmod(mode)
            result = self.run_script('delivery_config.py', 'show')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('secret', result.stdout + result.stderr)

    def test_symlink_config_is_rejected_for_read_and_write(self):
        target = self.home / 'target'
        target.write_text('secret')
        target.chmod(0o600)
        self.config.symlink_to(target)
        self.assertNotEqual(self.run_script('delivery_config.py', 'show').returncode, 0)
        self.assertNotEqual(self.configure().returncode, 0)
        self.assertEqual(target.read_text(), 'secret')

    def test_connector_selection_and_explicit_override_do_not_invoke_gws(self):
        self.assertEqual(self.configure('configured', 'gws').returncode, 0)
        fake_bin = self.home / 'bin'
        fake_bin.mkdir()
        marker = self.home / 'invoked'
        gws = fake_bin / 'gws'
        gws.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
        gws.chmod(0o755)
        self.env['PATH'] = str(fake_bin) + os.pathsep + self.env['PATH']
        result = self.run_script('drive_export.py', '--package-dir', self.home / 'package', '--drive-folder-id', 'override', '--drive-transport', 'connector')
        self.assertIn('connector_export.py next --package-dir', result.stdout)
        self.assertNotIn('NOT CONFIGURED', result.stdout)
        self.assertIn('--drive-folder-id override', result.stdout)
        self.assertFalse(marker.exists())

    def test_resolution_overrides_each_saved_value_independently(self):
        self.assertEqual(self.configure('configured', 'connector').returncode, 0)
        spec = importlib.util.spec_from_file_location('delivery_config_test', SCRIPTS / 'delivery_config.py')
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        from unittest.mock import patch
        with patch.dict(os.environ, {'CODEX_HOME': str(self.home)}):
            self.assertEqual(config.resolve_delivery('override'), {'drive_folder_id': 'override', 'transport': 'connector'})
            self.assertEqual(config.resolve_delivery(transport='gws'), {'drive_folder_id': 'configured', 'transport': 'gws'})

    def test_delivery_cli_rejects_malformed_config_without_exposing_it(self):
        self.config.write_text('{secret')
        self.config.chmod(0o600)
        result = self.run_script('drive_export.py', '--package-dir', self.home / 'package')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('secret', result.stdout + result.stderr)

    def test_list_turns_ignores_malformed_config(self):
        self.config.write_text('{bad')
        self.config.chmod(0o600)
        sessions = self.home / 'sessions'
        write_jsonl(sessions / 'root.jsonl', root_rows()[:8])
        result = self.run_script('audit_usage.py', '--list-turns', '--session-id', 'root', '--sessions-dir', sessions)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['session_id'], 'root')

    def test_malformed_config_blocks_delivery_but_keeps_valid_local_bundle(self):
        self.config.write_text('{secret')
        self.config.chmod(0o600)
        sessions = self.home / 'sessions'
        write_jsonl(sessions / 'root.jsonl', root_rows()[:8])
        output = self.home / 'out'
        result = self.run_script('audit_usage.py', '--session-id', 'root', '--turn-id', 'turn-a', '--sessions-dir', sessions, '--output-dir', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Delivery: blocked', result.stdout)
        self.assertNotIn('secret', result.stdout + result.stderr)
        manifests = list(output.rglob('manifest.json'))
        self.assertEqual(len(manifests), 1)
        spec = importlib.util.spec_from_file_location('bundle_test', SCRIPTS / 'bundle.py')
        bundle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bundle)
        self.assertTrue(bundle.validate_bundle(manifests[0].parent))

    def test_audit_uses_persisted_connector_destination(self):
        self.assertEqual(self.configure().returncode, 0)
        sessions = self.home / 'sessions'
        write_jsonl(sessions / 'root.jsonl', root_rows()[:8])
        result = self.run_script('audit_usage.py', '--session-id', 'root', '--turn-id', 'turn-a', '--sessions-dir', sessions, '--output-dir', self.home / 'out')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('connector_export.py next --package-dir', result.stdout)
        self.assertNotIn('NOT CONFIGURED', result.stdout)

    def test_auto_prefers_installed_gws_and_falls_back_when_missing(self):
        self.assertEqual(self.configure('configured', 'auto').returncode, 0)
        spec = importlib.util.spec_from_file_location('delivery_config_auto_test', SCRIPTS / 'delivery_config.py')
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        from unittest.mock import patch
        with patch.dict(os.environ, {'CODEX_HOME': str(self.home)}):
            with patch('shutil.which', return_value='/installed/gws'):
                self.assertEqual(config.resolve_delivery(), {'drive_folder_id': 'configured', 'transport': 'gws'})
                self.assertEqual(config.resolve_delivery(transport='connector')['transport'], 'connector')
            with patch('shutil.which', return_value=None):
                self.assertEqual(config.resolve_delivery(), {'drive_folder_id': 'configured', 'transport': 'connector'})
                self.assertEqual(config.resolve_delivery(transport='gws')['transport'], 'gws')

    def test_configure_defaults_to_auto_and_missing_gws_uses_plugin_handoff(self):
        result = self.run_script('delivery_config.py', 'configure', '--drive-folder-id', 'configured')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.config.read_text())['transport'], 'auto')
        self.env['PATH'] = str(self.home / 'no-executables')
        result = self.run_script('drive_export.py', '--package-dir', self.home / 'package')
        self.assertIn('connector_export.py next', result.stdout)
        self.assertIn('--drive-folder-id configured', result.stdout)

    def test_runtime_gws_auth_failure_hands_off_from_both_clis(self):
        from test_bundle import fixture_report, RATES
        import bundle
        self.assertEqual(self.configure('configured', 'gws').returncode, 0)
        fake_bin = self.home / 'bin'; fake_bin.mkdir()
        fake = fake_bin / 'gws'; fake.write_text('#!/bin/sh\necho private-auth-detail >&2\nexit 2\n'); fake.chmod(0o755)
        self.env['PATH'] = str(fake_bin) + os.pathsep + self.env['PATH']
        package = self.home / 'package'
        bundle.write_bundle(fixture_report(), package, rates_path=RATES, run_id='fixture-run')
        result = self.run_script('drive_export.py', '--package-dir', package)
        self.assertIn('connector_export.py next --package-dir', result.stdout)
        self.assertIn('--drive-folder-id configured', result.stdout)
        self.assertNotIn('private-auth-detail', result.stdout + result.stderr)
        sessions = self.home / 'sessions'; write_jsonl(sessions / 'root.jsonl', root_rows()[:8])
        result = self.run_script('audit_usage.py', '--session-id', 'root', '--turn-id', 'turn-a', '--sessions-dir', sessions, '--output-dir', self.home / 'out')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('connector_export.py next --package-dir', result.stdout)
        self.assertIn('--drive-folder-id configured', result.stdout)
        self.assertNotIn('private-auth-detail', result.stdout + result.stderr)

    def test_no_config_also_uses_gws_availability(self):
        spec = importlib.util.spec_from_file_location('delivery_config_default_test', SCRIPTS / 'delivery_config.py')
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        from unittest.mock import patch
        with patch.dict(os.environ, {'CODEX_HOME': str(self.home)}), patch('shutil.which', return_value=None):
            self.assertEqual(config.resolve_delivery(), {'drive_folder_id': None, 'transport': 'connector'})
