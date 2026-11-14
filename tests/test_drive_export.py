import hashlib
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/codex-token-audit/scripts/drive_export.py'


def load_module():
    spec = importlib.util.spec_from_file_location('drive_export', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeDrive:
    def __init__(self):
        self.objects = {'destination': {'id': 'destination', 'name': 'target', 'mimeType': 'application/vnd.google-apps.folder', 'capabilities': {'canAddChildren': True}, 'shared': False}}
        self.fail_name = None
        self.ambiguous_name = None
        self.hide_name = None
        self.serial = 0

    def get(self, file_id):
        return dict(self.objects[file_id])

    def list(self, parent_id, name):
        return [dict(x) for x in self.objects.values() if parent_id in x.get('parents', []) and x.get('name') == name and name != self.hide_name]

    def _create(self, parent, name, run_id, path=None):
        if self.fail_name == name:
            self.fail_name = None
            raise RuntimeError('secret backend detail')
        self.serial += 1
        file_id = 'id' + str(self.serial)
        data = {'id': file_id, 'name': name, 'parents': [parent], 'appProperties': {'tokenAuditRunId': run_id}, 'webViewLink': 'https://drive.google.com/drive/folders/' + file_id}
        if path is None:
            data.update(mimeType='application/vnd.google-apps.folder', shared=False, capabilities={'canAddChildren': True})
        else:
            content = Path(path).read_bytes()
            data.update(mimeType='application/octet-stream', size=str(len(content)), sha256Checksum=hashlib.sha256(content).hexdigest())
        self.objects[file_id] = data
        if self.ambiguous_name == name:
            self.ambiguous_name = None
            raise RuntimeError('secret backend detail')
        return dict(data)

    def create_folder(self, parent_id, name, run_id):
        return self._create(parent_id, name, run_id)

    def upload(self, parent_id, path, name, run_id):
        return self._create(parent_id, name, run_id, path)


class DriveExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config_env = mock.patch.dict(os.environ, {'CODEX_HOME': self.tmp.name})
        config_env.start()
        self.addCleanup(config_env.stop)
        self.package = Path(self.tmp.name) / 'run'
        self.package.mkdir(mode=0o700)
        names = ['audit.json', 'requests.jsonl', 'events.jsonl', 'schema.json', 'pricing.json']
        self.inventory = []
        for name in names:
            data = b'{}\n'
            p = self.package / name
            p.write_bytes(data)
            p.chmod(0o600)
            self.inventory.append({'name': name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'record_count': 1})
        self.manifest = {'schema_version': 2, 'run_id': 'run-123', 'files': self.inventory}
        p = self.package / 'manifest.json'
        p.write_text(json.dumps(self.manifest))
        p.chmod(0o600)
        self.drive = FakeDrive()

    def deliver(self, folder='destination', **kwargs):
        module = load_module()
        # Bundle validation is exercised by integration tests; this fixture supplies its contract.
        with mock.patch.object(module, 'validate_bundle', return_value=self.manifest):
            return module.upload_bundle(self.package, folder, self.drive, **kwargs)

    def test_success_and_retry_reuse_objects(self):
        first = self.deliver()
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(len(first['files']), 6)
        before = dict(self.drive.objects)
        self.assertEqual(self.deliver()['status'], 'complete')
        self.assertEqual(self.drive.objects, before)
        self.assertEqual((self.package / 'delivery-receipt.json').stat().st_mode & 0o777, 0o600)

    def test_gws_rejects_connector_pending_receipt_without_calls_or_changes(self):
        state = {'run_id': 'run-123', 'destination_id': 'destination',
                 'manifest_sha256': hashlib.sha256((self.package / 'manifest.json').read_bytes()).hexdigest(),
                 'transport': 'connector', 'status': 'in progress', 'files': {},
                 'pending': {'action': 'create_folder', 'name': 'token-audit-run-123', 'parent_id': 'destination'}}
        receipt = self.package / 'delivery-receipt.json'
        receipt.write_text(json.dumps(state))
        receipt.chmod(0o600)
        original = receipt.read_bytes()
        module = load_module()
        transport = mock.Mock(wraps=self.drive)
        with mock.patch.object(module, 'validate_bundle', return_value=self.manifest):
            result = module.upload_bundle(self.package, 'destination', transport)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result.get('fallback_transport'), 'connector')
        self.assertEqual(transport.mock_calls, [])
        self.assertEqual(receipt.read_bytes(), original)

    def test_legacy_gws_receipt_remains_resumable(self):
        self.assertEqual(self.deliver()['status'], 'complete')
        receipt = self.package / 'delivery-receipt.json'
        state = json.loads(receipt.read_text())
        state.pop('transport', None)
        receipt.write_text(json.dumps(state))
        before = dict(self.drive.objects)
        self.assertEqual(self.deliver()['status'], 'complete')
        self.assertEqual(self.drive.objects, before)

    def test_partial_and_ambiguous_retry_reconciles(self):
        self.drive.ambiguous_name = 'events.jsonl'
        self.assertNotEqual(self.deliver()['status'], 'complete')
        self.assertEqual(self.deliver()['status'], 'complete')
        self.assertEqual(sum(x.get('name') == 'events.jsonl' for x in self.drive.objects.values()), 1)

    def test_known_failure_can_retry_without_duplicate(self):
        module = load_module()
        original = self.drive.upload
        calls = []
        def fail_once(parent, path, name, run_id):
            if name == 'events.jsonl' and not calls:
                calls.append(name)
                raise module.DefinitiveFailure('request rejected before upload')
            return original(parent, path, name, run_id)
        self.drive.upload = fail_once
        with mock.patch.object(module, 'validate_bundle', return_value=self.manifest):
            self.assertNotEqual(module.upload_bundle(self.package, 'destination', self.drive)['status'], 'complete')
            self.assertEqual(module.upload_bundle(self.package, 'destination', self.drive)['status'], 'complete')
        self.assertEqual(sum(x.get('name') == 'events.jsonl' for x in self.drive.objects.values()), 1)

    def test_ambiguous_write_absent_from_listing_stops(self):
        self.drive.ambiguous_name = 'audit.json'
        self.deliver()
        self.drive.hide_name = 'audit.json'
        self.assertNotEqual(self.deliver()['status'], 'complete')
        self.assertEqual(sum(x.get('name') == 'audit.json' for x in self.drive.objects.values()), 1)

    def test_missing_configuration_never_calls_transport(self):
        result = self.deliver(folder=None)
        self.assertEqual(result['status'], 'NOT CONFIGURED')
        self.assertEqual(len(self.drive.objects), 1)

    def test_folder_guards_and_no_permissions_changes(self):
        for change in ({'mimeType': 'text/plain'}, {'capabilities': {'canAddChildren': False}}, {'shared': True}, {'shared': None}):
            with self.subTest(change=change):
                self.drive.objects['destination'].update(change)
                self.assertNotEqual(self.deliver()['status'], 'complete')
                self.assertEqual(len(self.drive.objects), 1)
                self.drive = FakeDrive()
        self.drive.objects['destination']['shared'] = True
        self.assertEqual(self.deliver(allow_shared=True)['status'], 'complete')
        self.assertTrue(self.drive.objects['destination']['shared'])

    def test_collision_or_remote_size_hash_mismatch_fails_closed(self):
        self.assertEqual(self.deliver()['status'], 'complete')
        item = next(x for x in self.drive.objects.values() if x.get('name') == 'audit.json')
        item['size'] = '99'
        self.assertNotEqual(self.deliver()['status'], 'complete')
        item['size'] = '3'
        item['sha256Checksum'] = '0' * 64
        self.assertNotEqual(self.deliver()['status'], 'complete')

    def test_metadata_only_is_explicit(self):
        original = self.drive.get
        def no_checksum(file_id):
            item = original(file_id)
            item.pop('sha256Checksum', None)
            return item
        self.drive.get = no_checksum
        receipt = self.deliver()
        self.assertEqual(receipt['files']['audit.json']['verification'], 'metadata readback only (not cryptographic)')

    def test_gws_adapter_uses_safe_arguments_and_follows_pages(self):
        module = load_module()
        responses = [subprocess.CompletedProcess([], 0, json.dumps({'files': [{'id': '1'}], 'nextPageToken': 'next'}), ''),
                     subprocess.CompletedProcess([], 0, json.dumps({'files': [{'id': '2'}]}), '')]
        with mock.patch.object(module.subprocess, 'run', side_effect=responses) as run:
            objects = module.GwsTransport().list('parent', "o'hara")
        self.assertEqual([obj['id'] for obj in objects], ['1', '2'])
        params = json.loads(run.call_args_list[1].args[0][-1])
        self.assertEqual(params['pageToken'], 'next')
        self.assertIn("o\\'hara", params['q'])
        self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_gws_folder_creation_tags_cross_app_visible_run_identity(self):
        module = load_module()
        response = subprocess.CompletedProcess([], 0, json.dumps({'id': 'folder'}), '')
        with mock.patch.object(module.subprocess, 'run', return_value=response) as run:
            module.GwsTransport().create_folder('destination', 'token-audit-run-123', 'run-123')
        args = run.call_args.args[0]
        body = json.loads(args[args.index('--json') + 1])
        self.assertEqual(body['properties'], {'tokenAuditRunId': 'run-123'})
        self.assertEqual(body['appProperties'], {'tokenAuditRunId': 'run-123'})
        self.assertIn('properties', json.loads(args[args.index('--params') + 1])['fields'])

    def test_incomplete_remote_listing_fails_closed(self):
        module = load_module()
        response = subprocess.CompletedProcess([], 0, json.dumps({'files': [], 'incompleteSearch': True}), '')
        with mock.patch.object(module.subprocess, 'run', return_value=response):
            with self.assertRaises(module.DeliveryError):
                module.GwsTransport().list('parent', 'name')

    def test_readback_must_match_listed_identity(self):
        self.assertEqual(self.deliver()['status'], 'complete')
        (self.package / 'delivery-receipt.json').unlink()
        original = self.drive.get
        def wrong_identity(file_id):
            item = original(file_id)
            if item.get('name') == 'audit.json':
                item['id'] = 'different-id'
            return item
        self.drive.get = wrong_identity
        self.assertNotEqual(self.deliver()['status'], 'complete')

    def test_resumed_shared_run_folder_blocks_upload_and_records_failure(self):
        self.drive.ambiguous_name = 'events.jsonl'
        self.assertEqual(self.deliver()['status'], 'blocked')
        folder = next(x for x in self.drive.objects.values() if x.get('name') == 'token-audit-run-123')
        folder['shared'] = True
        before = dict(self.drive.objects)
        self.assertEqual(self.deliver()['status'], 'blocked')
        self.assertEqual(before, self.drive.objects)
        receipt = json.loads((self.package / 'delivery-receipt.json').read_text())
        self.assertEqual(receipt['status'], 'blocked')
        self.assertEqual(receipt['pending'], 'events.jsonl')
        self.assertTrue(receipt['files'])
        self.assertEqual(self.deliver(allow_shared=True)['status'], 'complete')

    def test_folder_is_rechecked_before_each_upload(self):
        original = self.drive.upload
        def switch_after_upload(parent, path, name, run_id):
            result = original(parent, path, name, run_id)
            self.drive.objects[parent]['shared'] = True
            return result
        self.drive.upload = switch_after_upload
        self.assertEqual(self.deliver()['status'], 'blocked')
        self.assertEqual(sum('size' in item for item in self.drive.objects.values()), 1)

    def test_unknown_or_unwritable_subfolder_fails_closed(self):
        self.drive.ambiguous_name = 'audit.json'
        self.deliver()
        folder = next(x for x in self.drive.objects.values() if x.get('name') == 'token-audit-run-123')
        for change in ({'shared': None}, {'capabilities': {'canAddChildren': False}}, {'driveId': 'shared-drive'}):
            with self.subTest(change=change):
                original = dict(folder)
                folder.update(change)
                self.assertEqual(self.deliver()['status'], 'blocked')
                folder.clear()
                folder.update(original)

    def test_cli_output_is_compact_and_omits_file_inventory(self):
        module = load_module()
        result = {'status': 'complete', 'folder_link': 'https://drive.example/folder', 'files': {'secret-name': {'id': 'private'}}}
        out = io.StringIO()
        with mock.patch.object(module, 'upload_bundle', return_value=result), mock.patch('sys.argv', ['drive_export', '--package-dir', str(self.package)]), contextlib.redirect_stdout(out):
            self.assertEqual(module.main(), 0)
        self.assertIn('https://drive.example/folder', out.getvalue())
        self.assertNotIn('secret-name', out.getvalue())
        self.assertLessEqual(len(out.getvalue().splitlines()), 4)

    def test_shared_drive_requires_explicit_override(self):
        self.drive.objects['destination']['driveId'] = 'shared-drive'
        self.assertNotEqual(self.deliver()['status'], 'complete')
        self.assertEqual(len(self.drive.objects), 1)

    def test_failed_reverification_does_not_leave_completed_receipt(self):
        self.assertEqual(self.deliver()['status'], 'complete')
        item = next(x for x in self.drive.objects.values() if x.get('name') == 'audit.json')
        item['size'] = '99'
        self.assertNotEqual(self.deliver()['status'], 'complete')
        receipt = json.loads((self.package / 'delivery-receipt.json').read_text())
        self.assertNotEqual(receipt['status'], 'complete')

    def test_local_changes_and_symlinks_rejected(self):
        (self.package / 'audit.json').write_bytes(b'changed')
        self.assertNotEqual(self.deliver()['status'], 'complete')
        self.assertEqual(len(self.drive.objects), 1)
        (self.package / 'audit.json').unlink()
        (self.package / 'audit.json').symlink_to(self.package / 'pricing.json')
        self.assertNotEqual(self.deliver()['status'], 'complete')

    def test_transport_delivery_exception_does_not_disclose_backend_detail(self):
        module = load_module()
        self.drive.get = lambda _: (_ for _ in ()).throw(module.DeliveryError('secret backend detail'))
        with mock.patch.object(module, 'validate_bundle', return_value=self.manifest):
            result = module.upload_bundle(self.package, 'destination', self.drive)
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('secret', (self.package / 'delivery-receipt.json').read_text())

    def test_transport_failures_offer_fallback_but_safety_failures_do_not(self):
        module = load_module()
        for failure in (module.DefinitiveFailure('gws unavailable'), module.TransportFailure('gws request failed')):
            with self.subTest(failure=failure):
                self.drive.get = mock.Mock(side_effect=failure)
                with mock.patch.object(module, 'validate_bundle', return_value=self.manifest):
                    result = module.upload_bundle(self.package, 'destination', self.drive)
                self.assertEqual(result.get('fallback_transport'), 'connector')
        self.drive = FakeDrive()
        self.drive.objects['destination']['shared'] = True
        self.assertNotIn('fallback_transport', self.deliver())

    def test_gws_timeout_is_bounded_and_a_transport_failure(self):
        module = load_module()
        with mock.patch.object(module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gws', 30)) as run:
            with self.assertRaises(module.TransportFailure):
                module.GwsTransport().get('destination')
        self.assertEqual(run.call_args.kwargs['timeout'], 30)

    def test_malformed_listing_and_create_responses_request_connector_handoff(self):
        module = load_module()
        response = subprocess.CompletedProcess([], 0, json.dumps({'files': 'not-a-list'}), '')
        with mock.patch.object(module.subprocess, 'run', return_value=response):
            with self.assertRaises(module.TransportFailure):
                module.GwsTransport().list('destination', 'name')
        with mock.patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')):
            with self.assertRaises(module.TransportFailure):
                module.GwsTransport().get('destination')
        self.drive.create_folder = mock.Mock(return_value={})
        with mock.patch.object(module, 'validate_bundle', return_value=self.manifest):
            result = module.upload_bundle(self.package, 'destination', self.drive)
        self.assertEqual(result.get('fallback_transport'), 'connector')
        state = json.loads((self.package / 'delivery-receipt.json').read_text())
        self.assertEqual(state['pending'], 'token-audit-run-123')

    def test_pending_ambiguous_mutation_keeps_state_during_handoff(self):
        module = load_module()
        self.drive.create_folder = mock.Mock(side_effect=module.TransportFailure('gws request failed'))
        with mock.patch.object(module, 'validate_bundle', return_value=self.manifest):
            result = module.upload_bundle(self.package, 'destination', self.drive)
        self.assertEqual(result.get('fallback_transport'), 'connector')
        state = json.loads((self.package / 'delivery-receipt.json').read_text())
        self.assertEqual(state['pending'], 'token-audit-run-123')

    def test_auth_error_does_not_disclose_backend_detail(self):
        self.drive.get = lambda _: (_ for _ in ()).throw(RuntimeError('secret backend detail'))
        result = self.deliver()
        self.assertNotEqual(result['status'], 'complete')
        self.assertNotIn('secret', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
