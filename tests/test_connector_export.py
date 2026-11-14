import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from test_bundle import SCRIPTS, RATES, fixture_report
import bundle

SCRIPT = SCRIPTS / 'connector_export.py'
FOLDER = 'application/vnd.google-apps.folder'


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'bundle'
        bundle.write_bundle(fixture_report(), self.package, rates_path=RATES, run_id='fixture-run')
        self.destination = self.meta('dest', 'destination', FOLDER, [])
        self.folder = self.meta('folder', 'token-audit-fixture-run', FOLDER, ['dest'])

    def meta(self, ident, name, mime, parents):
        return {'id': ident, 'title': name, 'mime_type': mime, 'parent_ids': parents,
                'shared': False, 'drive_id': None, 'url': 'https://example.invalid/' + ident}

    def invoke(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), *args, '--package-dir', str(self.package)],
                                text=True, capture_output=True)
        self.assertTrue(result.stdout, result.stderr)
        return json.loads(result.stdout)

    def metadata_file(self, value):
        path = self.root / 'metadata.json'
        path.write_text(json.dumps(value))
        return str(path)

    def next(self, folder=None, destination=None, dest_id='dest'):
        args = ['next', '--drive-folder-id', dest_id, '--destination-metadata', self.metadata_file(destination or self.destination)]
        if folder is not None:
            # Do not reuse the destination metadata path.
            path = self.root / 'folder.json'; path.write_text(json.dumps(folder))
            args += ['--folder-metadata', str(path)]
        return self.invoke(*args)

    def record(self, ident, metadata, downloaded=None):
        args = ['record', '--created-id', ident, '--metadata', self.metadata_file(metadata)]
        if downloaded is not None:
            args += ['--downloaded-file', str(downloaded)]
        return self.invoke(*args)

    def create_folder(self):
        self.assertEqual(self.next()['action'], 'create_folder')
        self.assertEqual(self.record('folder', self.folder)['status'], 'in progress')

    def gws_state(self, **updates):
        state = {'run_id': 'fixture-run', 'destination_id': 'dest',
                 'manifest_sha256': hashlib.sha256((self.package / 'manifest.json').read_bytes()).hexdigest(),
                 'transport': 'gws', 'files': {}, 'pending': None, 'status': 'blocked'}
        state.update(updates)
        receipt = self.package / 'delivery-receipt.json'
        receipt.write_text(json.dumps(state, indent=1)); receipt.chmod(0o600)
        return state

    def test_gws_prewrite_receipt_migrates_and_preserves_original(self):
        original = self.gws_state()
        self.assertEqual(self.next()['action'], 'create_folder')
        receipt = json.loads((self.package / 'delivery-receipt.json').read_text())
        self.assertEqual(receipt['previous_gws_delivery'], original)
        self.assertEqual(self.next()['action'], 'reconcile_pending')

    def test_gws_known_folder_requires_readback_and_preserves_pending_raw_write(self):
        original = self.gws_state(folder_id='folder', pending='events.jsonl',
                                  files={'audit.json': {'id': 'raw-id', 'verification': 'sha256'}})
        receipt = self.package / 'delivery-receipt.json'; before = receipt.read_bytes()
        action = self.next()
        self.assertEqual(action['action'], 'read_gws_folder')
        self.assertEqual(action['folder_id'], 'folder')
        self.assertEqual(receipt.read_bytes(), before)
        action = self.next(self.folder)
        self.assertEqual(action['action'], 'upload_file')
        self.assertTrue(action['name'].endswith('.zip'))
        migrated = json.loads(receipt.read_text())
        self.assertEqual(migrated['previous_gws_delivery'], original)
        self.assertEqual(self.next(self.folder)['action'], 'reconcile_pending')

    def test_gws_unknown_pending_folder_is_read_only_until_observed(self):
        original = self.gws_state(pending='token-audit-fixture-run')
        receipt = self.package / 'delivery-receipt.json'; before = receipt.read_bytes()
        action = self.next()
        self.assertEqual(action['action'], 'reconcile_gws_folder')
        self.assertEqual((action['parent_id'], action['name'], action['run_id']), ('dest', 'token-audit-fixture-run', 'fixture-run'))
        self.assertEqual(action['property'], {'field': 'properties', 'key': 'tokenAuditRunId', 'value': 'fixture-run'})
        self.assertEqual(receipt.read_bytes(), before)
        bad = {**self.folder, 'parent_ids': ['wrong']}
        self.assertEqual(self.next(bad)['status'], 'blocked')
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(self.next(self.folder)['action'], 'upload_file')
        self.assertEqual(json.loads(receipt.read_text())['previous_gws_delivery'], original)

    def test_invalid_gws_receipt_remains_byte_identical(self):
        cases = [dict(pending=42), dict(files={'audit.json': {'id': 42, 'verification': 'sha256'}}),
                 dict(folder_id='dest'), dict(folder_link={'bad': 'value'}), dict(transport='unknown')]
        for change in cases:
            with self.subTest(change=change):
                self.gws_state(**change)
                receipt = self.package / 'delivery-receipt.json'; before = receipt.read_bytes()
                self.assertEqual(self.next()['status'], 'blocked')
                self.assertEqual(receipt.read_bytes(), before)

    def test_gws_complete_and_legacy_states_and_identity_mismatch(self):
        names = ['audit.json', 'requests.jsonl', 'events.jsonl', 'schema.json', 'pricing.json', 'manifest.json']
        state = self.gws_state(folder_id='folder', status='complete', files={n: {'id': 'id-' + n, 'verification': 'sha256'} for n in names})
        state.pop('transport'); receipt = self.package / 'delivery-receipt.json'; receipt.write_text(json.dumps(state))
        before = receipt.read_bytes()
        self.assertEqual(self.next()['status'], 'complete')
        self.assertEqual(receipt.read_bytes(), before)
        state['destination_id'] = 'other'; receipt.write_text(json.dumps(state)); before = receipt.read_bytes()
        self.assertEqual(self.next()['status'], 'blocked')
        self.assertEqual(receipt.read_bytes(), before)

    def test_archive_preserves_empty_files_and_requires_separate_readback(self):
        self.create_folder()
        action = self.next(self.folder)
        self.assertEqual(action['action'], 'upload_file')
        self.assertEqual(action['name'], 'token-audit-fixture-run.zip')
        archive = Path(action['local_path'])
        self.assertEqual(os.stat(archive).st_mode & 0o777, 0o600)
        names = {'audit.json', 'requests.jsonl', 'events.jsonl', 'schema.json', 'pricing.json', 'manifest.json'}
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(set(zipped.namelist()), names)
            self.assertEqual(zipped.read('events.jsonl'), b'')
            for name in names:
                self.assertEqual(zipped.read(name), (self.package / name).read_bytes())
        self.assertEqual(self.next(self.folder)['action'], 'reconcile_pending')
        item = self.meta('archive-id', action['name'], 'application/zip', ['folder'])
        self.assertEqual(self.record('archive-id', item)['status'], 'blocked')
        fake = self.root / 'fake-download'; fake.write_bytes(archive.read_bytes())
        self.assertEqual(self.record('archive-id', {**item, 'mime_type': 'text/plain'}, fake)['status'], 'blocked')
        self.assertEqual(self.record('archive-id', item, archive)['status'], 'blocked')
        self.assertEqual(self.record('archive-id', item, self.package / 'audit.json')['status'], 'blocked')
        downloaded = self.root / 'download'; downloaded.write_bytes(archive.read_bytes())
        self.assertEqual(self.record('archive-id', item, downloaded)['status'], 'in progress')
        self.assertEqual(self.next(self.folder)['status'], 'complete')
        receipt = json.loads((self.package / 'delivery-receipt.json').read_text())
        self.assertEqual(receipt['status'], 'complete')
        self.assertEqual(set(receipt['files']), names)
        for name, entry in receipt['files'].items():
            self.assertEqual(entry['id'], 'archive-id')
            self.assertEqual(entry['size_bytes'], (self.package / name).stat().st_size)
            self.assertEqual(entry['verification'], 'sha256')
        for name in ('delivery-receipt.json', '.delivery.lock'):
            self.assertEqual(os.stat(self.package / name).st_mode & 0o777, 0o600)

    def test_archives_of_identical_bundle_are_deterministic(self):
        second = self.root / 'copy'
        shutil.copytree(self.package, second)
        self.create_folder()
        first = Path(self.next(self.folder)['local_path']).read_bytes()
        self.package = second
        self.create_folder()
        other = Path(self.next(self.folder)['local_path']).read_bytes()
        self.assertEqual(first, other)

    def test_changed_prepared_archive_blocks_reconciliation(self):
        self.create_folder()
        action = self.next(self.folder)
        Path(action['local_path']).write_bytes(b'corrupt')
        self.assertEqual(self.next(self.folder)['status'], 'blocked')
        item = self.meta('file', action['name'], 'application/zip', ['folder'])
        self.assertEqual(self.record('file', item, self.root / 'missing')['status'], 'blocked')

    def test_pending_mutation_is_never_repeated(self):
        first = self.next()
        self.assertEqual(first['action'], 'create_folder')
        self.assertEqual(self.next()['action'], 'reconcile_pending')

    def test_metadata_mismatch_never_acknowledges_create(self):
        self.next()
        for key, value in [('id', 'different'), ('title', 'wrong'), ('parent_ids', ['other']),
                           ('mime_type', 'text/plain'), ('shared', True), ('shared', None), ('drive_id', 'shared-drive'), ('trashed', True)]:
            metadata = dict(self.folder); metadata[key] = value
            self.assertEqual(self.record('folder', metadata)['status'], 'blocked', key)
        self.assertEqual(self.next()['action'], 'reconcile_pending')

    def test_bad_download_and_changed_bundle_are_blocked(self):
        self.create_folder()
        action = self.next(self.folder); name = action['name']
        metadata = self.meta('file', name, 'application/octet-stream', ['folder'])
        downloaded = self.root / 'download'; downloaded.write_bytes(Path(action['local_path']).read_bytes())
        self.assertEqual(self.record('file', {**metadata, 'size': '999999999'}, downloaded)['status'], 'blocked')
        bad = self.root / 'bad'; bad.write_bytes(b'wrong')
        self.assertEqual(self.record('file', metadata, bad)['status'], 'blocked')
        (self.package / 'audit.json').write_bytes(b'changed')
        self.assertEqual(self.record('file', metadata, bad)['status'], 'blocked')
        self.assertEqual(self.next(self.folder)['status'], 'blocked')

    def test_lock_and_other_transport_state_block_delivery(self):
        self.assertEqual(self.next()['action'], 'create_folder')
        with (self.package / '.delivery.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.next()['status'], 'blocked')
        receipt_path = self.package / 'delivery-receipt.json'
        receipt = json.loads(receipt_path.read_text()); receipt['transport'] = 'gws'
        receipt_path.write_text(json.dumps(receipt))
        self.assertEqual(self.next()['status'], 'blocked')
        self.assertEqual(self.record('folder', self.folder)['status'], 'blocked')

    def test_destination_and_folder_are_revalidated_and_state_cannot_switch_destination(self):
        self.create_folder()
        bad = dict(self.folder); bad['shared'] = True
        self.assertEqual(self.next(bad)['status'], 'blocked')
        self.assertEqual(self.next(self.folder, dest_id='other', destination=self.meta('other', 'destination', FOLDER, []))['status'], 'blocked')
        self.assertEqual(self.next(self.folder, destination={**self.destination, 'shared': None})['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
