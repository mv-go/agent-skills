import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_bundle import SCRIPTS, RATES, fixture_report
import bundle
from test_drive_export import FakeDrive, load_module


class SetupDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'custom-codex-home'
        self.home.mkdir(mode=0o700)
        self.env = {**os.environ, 'CODEX_HOME': str(self.home)}
        self.script = SCRIPTS / 'setup_delivery.py'
        self.probe = self.root / 'probe'

    def run_setup(self, *args):
        return subprocess.run([sys.executable, str(self.script), *map(str, args)],
                              env=self.env, text=True, capture_output=True)

    def configure(self, folder='destination'):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'delivery_config.py'),
                                 'configure', '--drive-folder-id', folder, '--transport', 'auto'],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def make_probe(self):
        result = self.run_setup('probe', '--output-dir', self.probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'delivery_check_required')
        return bundle.validate_bundle(self.probe)

    def test_missing_destination_is_incomplete_and_creates_nothing(self):
        result = self.run_setup('status')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['status'], 'destination_required')
        result = self.run_setup('probe', '--output-dir', self.probe)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.probe.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_configured_host_requires_delivery_check_without_changing_config(self):
        self.configure()
        config = self.home / 'codex-token-audit.json'
        original = config.read_bytes()
        result = self.run_setup('status')
        self.assertEqual(result.returncode, 2)
        value = json.loads(result.stdout)
        self.assertEqual(value['status'], 'delivery_check_required')
        self.assertEqual(value['drive_folder_id'], 'destination')
        self.assertEqual(config.read_bytes(), original)

    def test_probe_is_synthetic_private_no_overwrite_and_never_reads_sessions(self):
        self.configure()
        sessions = self.home / 'sessions'
        sessions.mkdir()
        (sessions / 'secret.jsonl').write_text('PRIVATE_SETUP_SENTINEL')
        manifest = self.make_probe()
        self.assertEqual(manifest['scope']['session_id'], 'synthetic-install-check')
        self.assertTrue(manifest['run_id'].startswith('synthetic-install-'))
        self.assertEqual(self.probe.stat().st_mode & 0o777, 0o700)
        for path in self.probe.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b'PRIVATE_SETUP_SENTINEL', path.read_bytes())
        before = (self.probe / 'manifest.json').read_bytes()
        self.assertNotEqual(self.run_setup('probe', '--output-dir', self.probe).returncode, 0)
        self.assertEqual((self.probe / 'manifest.json').read_bytes(), before)

    def test_completed_gws_delivery_proves_ready_and_changed_destination_invalidates_it(self):
        self.configure()
        self.make_probe()
        receipt = load_module().upload_bundle(self.probe, 'destination', FakeDrive())
        self.assertEqual(receipt['status'], 'complete')
        result = self.run_setup('verify', '--package-dir', self.probe)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'ready')
        self.configure('different-destination')
        self.assertNotEqual(self.run_setup('verify', '--package-dir', self.probe).returncode, 0)

    def test_pending_missing_files_and_mismatched_manifest_never_prove_ready(self):
        self.configure()
        self.make_probe()
        self.assertNotEqual(self.run_setup('verify', '--package-dir', self.probe).returncode, 0)
        receipt = load_module().upload_bundle(self.probe, 'destination', FakeDrive())
        path = self.probe / 'delivery-receipt.json'
        for change in ({'status': 'blocked'}, {'pending': 'events.jsonl'}, {'files': {}},
                       {'manifest_sha256': '0' * 64}, {'run_id': 'unrelated'}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**receipt, **change}))
                result = self.run_setup('verify', '--package-dir', self.probe)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(json.loads(result.stdout)['status'], 'ready')

    def test_metadata_only_completed_upload_does_not_prove_ready(self):
        self.configure()
        self.make_probe()
        receipt = load_module().upload_bundle(self.probe, 'destination', FakeDrive())
        for item in receipt['files'].values():
            item['verification'] = 'metadata readback only (not cryptographic)'
        (self.probe / 'delivery-receipt.json').write_text(json.dumps(receipt))
        self.assertNotEqual(self.run_setup('verify', '--package-dir', self.probe).returncode, 0)

    def test_real_task_bundle_is_not_a_setup_probe(self):
        self.configure()
        bundle.write_bundle(fixture_report(), self.probe, rates_path=RATES, run_id='real-task')
        self.assertEqual(load_module().upload_bundle(self.probe, 'destination', FakeDrive())['status'], 'complete')
        self.assertNotEqual(self.run_setup('verify', '--package-dir', self.probe).returncode, 0)

    def test_synthetic_label_does_not_turn_real_records_into_a_setup_probe(self):
        self.configure()
        report = fixture_report()
        report['scope']['session_id'] = 'synthetic-install-check'
        bundle.write_bundle(report, self.probe, rates_path=RATES, run_id='synthetic-install-label-only')
        self.assertEqual(load_module().upload_bundle(self.probe, 'destination', FakeDrive())['status'], 'complete')
        self.assertNotEqual(self.run_setup('verify', '--package-dir', self.probe).returncode, 0)

    def test_connector_requires_independent_archive_readback_before_ready(self):
        from test_connector_export import ConnectorTests
        self.configure('dest')
        self.make_probe()
        manifest = bundle.validate_bundle(self.probe)
        workflow = ConnectorTests(methodName='test_pending_mutation_is_never_repeated')
        workflow.setUp()
        self.addCleanup(workflow.doCleanups)
        workflow.package = self.probe
        workflow.folder['title'] = 'token-audit-' + manifest['run_id']
        self.assertEqual(workflow.next()['action'], 'create_folder')
        self.assertNotEqual(self.run_setup('verify', '--package-dir', self.probe).returncode, 0)
        workflow.record('folder', workflow.folder)
        action = workflow.next(workflow.folder)
        metadata = workflow.meta('archive-id', action['name'], 'application/zip', ['folder'])
        self.assertEqual(workflow.record('archive-id', metadata)['status'], 'blocked')
        downloaded = self.root / 'independently-downloaded.zip'
        downloaded.write_bytes(Path(action['local_path']).read_bytes())
        workflow.record('archive-id', metadata, downloaded)
        self.assertEqual(workflow.next(workflow.folder)['status'], 'complete')
        self.assertEqual(self.run_setup('verify', '--package-dir', self.probe).returncode, 0)

    def test_isolated_copied_package_can_prepare_and_verify(self):
        self.configure()
        copied = self.root / 'isolated-skill'
        shutil.copytree(SCRIPTS.parent, copied)
        self.script = copied / 'scripts/setup_delivery.py'
        self.make_probe()
        self.assertEqual(load_module().upload_bundle(self.probe, 'destination', FakeDrive())['status'], 'complete')
        self.assertEqual(self.run_setup('verify', '--package-dir', self.probe).returncode, 0)


if __name__ == '__main__':
    unittest.main()
