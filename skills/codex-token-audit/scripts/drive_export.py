#!/usr/bin/env python3
"""Deliver an immutable token audit package with conservative retry semantics."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys
import stat
import subprocess
import tempfile

FOLDER = 'application/vnd.google-apps.folder'
FIELDS = 'id,name,mimeType,parents,size,sha256Checksum,md5Checksum,appProperties,properties,webViewLink,shared,driveId,capabilities(canAddChildren),trashed'


def validate_bundle(path):
    spec = importlib.util.spec_from_file_location('_token_audit_bundle', Path(__file__).with_name('bundle.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_bundle(path)


class DeliveryError(Exception):
    pass


class TransportFailure(DeliveryError):
    """The Drive CLI transport failed; remote mutation may be uncertain."""


class DefinitiveFailure(TransportFailure):
    """A transport guarantees no remote mutation occurred."""


class GwsTransport:
    """Uses existing gws credentials; never returns raw stderr to the caller."""
    def _run(self, method, params, body=None, upload=None):
        args = ['gws', 'drive', 'files', method, '--params', json.dumps(params)]
        if body is not None:
            args += ['--json', json.dumps(body)]
        cwd = None
        if upload is not None:
            upload = Path(upload)
            cwd = str(upload.parent)
            args += ['--upload', upload.name]
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False, timeout=30)
        except OSError:
            raise DefinitiveFailure('gws unavailable') from None
        except subprocess.TimeoutExpired:
            raise TransportFailure('gws request timed out') from None
        if result.returncode:
            if result.returncode in (2, 3):
                raise DefinitiveFailure('gws authentication or validation failed')
            raise TransportFailure('gws request failed')
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError('invalid response')
            return value
        except (ValueError, TypeError):
            raise TransportFailure('gws returned invalid response') from None

    def get(self, file_id):
        result = self._run('get', {'fileId': file_id, 'fields': FIELDS, 'supportsAllDrives': True})
        if not isinstance(result.get('id'), str) or not result['id']:
            raise TransportFailure('gws returned invalid response')
        return result

    def list(self, parent_id, name):
        def escape(value):
            return value.replace('\\', '\\\\').replace("'", "\\'")
        q = "'%s' in parents and name = '%s' and trashed = false" % (escape(parent_id), escape(name))
        params = {'q': q, 'fields': 'nextPageToken,incompleteSearch,files(%s)' % FIELDS, 'pageSize': 1000, 'supportsAllDrives': True, 'includeItemsFromAllDrives': True}
        files = []
        while True:
            page = self._run('list', params)
            if page.get('incompleteSearch') is True:
                raise TransportFailure('Drive listing incomplete')
            page_files = page.get('files', [])
            if not isinstance(page_files, list) or any(not isinstance(item, dict) for item in page_files):
                raise TransportFailure('gws returned invalid response')
            files.extend(page_files)
            token = page.get('nextPageToken')
            if token is not None and (not isinstance(token, str) or not token):
                raise TransportFailure('gws returned invalid response')
            if not token:
                return files
            params['pageToken'] = token

    def create_folder(self, parent_id, name, run_id):
        return self._run('create', {'fields': FIELDS, 'supportsAllDrives': True}, {'name': name, 'mimeType': FOLDER, 'parents': [parent_id], 'appProperties': {'tokenAuditRunId': run_id}, 'properties': {'tokenAuditRunId': run_id}})

    def upload(self, parent_id, path, name, run_id):
        return self._run('create', {'fields': FIELDS, 'supportsAllDrives': True}, {'name': name, 'parents': [parent_id], 'appProperties': {'tokenAuditRunId': run_id}}, path)


def _private(path, directory=False):
    s = path.lstat()
    if (not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)) or s.st_mode & 0o077):
        raise DeliveryError('local package or state is not private and regular')


def _digest(path):
    _private(path)
    h = hashlib.sha256()
    md5 = hashlib.md5()  # Drive may expose only its MD5 checksum.
    with path.open('rb') as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
            md5.update(chunk)
    return path.stat().st_size, h.hexdigest(), md5.hexdigest()


def _write_state(path, state):
    if path.exists() or path.is_symlink():
        _private(path)
    fd, temp = tempfile.mkstemp(prefix='.delivery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _check_remote(item, parent, name, run_id, local=None):
    if (not isinstance(item, dict) or not item.get('id') or item.get('name') != name
            or item.get('parents') != [parent] or item.get('trashed') is True
            or item.get('appProperties', {}).get('tokenAuditRunId') != run_id):
        raise DeliveryError('remote collision or metadata mismatch')
    if local is None:
        if item.get('mimeType') != FOLDER:
            raise DeliveryError('remote folder mismatch')
        return 'folder metadata'
    size, sha, md5 = local
    if item.get('mimeType', '').startswith('application/vnd.google-apps.') or str(item.get('size')) != str(size):
        raise DeliveryError('remote file size or type mismatch')
    if item.get('sha256Checksum'):
        if item['sha256Checksum'].lower() != sha:
            raise DeliveryError('remote checksum mismatch')
        return 'sha256'
    if item.get('md5Checksum'):
        if item['md5Checksum'].lower() != md5:
            raise DeliveryError('remote checksum mismatch')
        return 'md5'
    return 'metadata readback only (not cryptographic)'


def upload_bundle(package_dir, folder_id, transport=None, allow_shared=False):
    if not folder_id:
        return {'status': 'NOT CONFIGURED', 'reason': 'Drive folder ID is required'}
    path = Path(package_dir)
    try:
        _private(path, directory=True)
        lock_path = path / '.delivery.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            _private(lock_path)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _deliver(path, folder_id, transport or GwsTransport(), allow_shared)
        finally:
            os.close(fd)
    except (Exception,):
        return {'status': 'blocked', 'reason': 'local validation, lock, or Drive delivery failed'}


def _deliver(path, folder_id, transport, allow_shared):
    state_path = path / 'delivery-receipt.json'
    state = None
    try:
        manifest = validate_bundle(path)
        run_id = manifest['run_id']
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', run_id):
            raise DeliveryError('unsafe run ID')
        entries = manifest['files']
        if not isinstance(entries, list):
            raise DeliveryError('invalid inventory')
        expected = {}
        for entry in entries:
            name = entry['name']
            if not isinstance(name, str) or not name or Path(name).name != name or name in expected or name in ('manifest.json', 'delivery-receipt.json', '.delivery.lock'):
                raise DeliveryError('invalid inventory name')
            info = _digest(path / name)
            if info[0] != entry['size_bytes'] or info[1] != entry['sha256']:
                raise DeliveryError('local inventory mismatch')
            expected[name] = info
        expected['manifest.json'] = _digest(path / 'manifest.json')
        identity = expected['manifest.json'][1]
        if state_path.exists() or state_path.is_symlink():
            _private(state_path)
            saved = json.loads(state_path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                raise DeliveryError('delivery transport does not match existing receipt')
            if (saved.get('run_id'), saved.get('destination_id'), saved.get('manifest_sha256')) != (run_id, folder_id, identity):
                raise DeliveryError('delivery state does not match package or destination')
            if saved.get('transport') == 'connector':
                raise TransportFailure('delivery already uses connector')
            if saved.get('transport') not in (None, 'gws'):
                raise DeliveryError('delivery transport does not match existing receipt')
            state = saved
        else:
            state = {'run_id': run_id, 'destination_id': folder_id, 'manifest_sha256': identity, 'files': {}, 'pending': None}
        state['transport'] = 'gws'
        state['status'] = 'in progress'
        _write_state(state_path, state)
        def check_folder(item, file_id):
            if (item.get('id') != file_id or item.get('mimeType') != FOLDER or item.get('trashed') is True
                    or item.get('capabilities', {}).get('canAddChildren') is not True):
                raise DeliveryError('destination is not a writable folder')
            if not allow_shared and (item.get('shared') is not False or item.get('driveId')):
                raise DeliveryError('destination sharing is shared or unknown')

        check_folder(transport.get(folder_id), folder_id)

        def reconcile(parent, name, local=None):
            matches = transport.list(parent, name)
            if len(matches) > 1:
                raise DeliveryError('multiple remote matches')
            if matches:
                candidate = matches[0]
                if not candidate.get('id'):
                    raise DeliveryError('remote ID missing')
                item = transport.get(candidate['id'])
                if item.get('id') != candidate['id']:
                    raise DeliveryError('remote readback ID mismatch')
                level = _check_remote(item, parent, name, run_id, local)
                saved = state.get('folder_id') if local is None else state['files'].get(name, {}).get('id')
                if saved and saved != item['id']:
                    raise DeliveryError('remote ID changed')
                return item, level
            if state.get('pending') == name or (state.get('folder_id') if local is None else state['files'].get(name)):
                raise DeliveryError('remote object not visible; retry later without creating')
            state['pending'] = name
            _write_state(state_path, state)
            # A failed or interrupted mutation remains pending until it can be reconciled.
            try:
                if local is None:
                    created = transport.create_folder(parent, name, run_id)
                else:
                    created = transport.upload(parent, path / name, name, run_id)
            except DefinitiveFailure:
                state['pending'] = None
                _write_state(state_path, state)
                raise TransportFailure('Drive request rejected before upload') from None
            if not isinstance(created, dict) or not created.get('id'):
                raise TransportFailure('remote response lacks ID')
            item = transport.get(created['id'])
            if item.get('id') != created['id']:
                raise DeliveryError('remote readback ID mismatch')
            return item, _check_remote(item, parent, name, run_id, local)

        folder_name = 'token-audit-' + run_id
        folder, _ = reconcile(folder_id, folder_name)
        check_folder(folder, folder['id'])
        state['folder_id'] = folder['id']
        if state.get('pending') == folder_name:
            state['pending'] = None
        if folder.get('webViewLink'):
            state['folder_link'] = folder['webViewLink']
        _write_state(state_path, state)
        for name, original in expected.items():
            check_folder(transport.get(folder_id), folder_id)
            current_folder = transport.get(folder['id'])
            _check_remote(current_folder, folder_id, folder_name, run_id)
            check_folder(current_folder, folder['id'])
            # Validate the entire package on every write and verify the manifest identity.
            validate_bundle(path)
            if _digest(path / 'manifest.json')[1] != identity or _digest(path / name) != original:
                raise DeliveryError('local package changed during delivery')
            item, level = reconcile(folder['id'], name, original)
            record = {'id': item['id'], 'verification': level}
            if item.get('webViewLink'):
                record['link'] = item['webViewLink']
            state['files'][name] = record
            if state.get('pending') == name:
                state['pending'] = None
            _write_state(state_path, state)
        state['status'] = 'complete'
        _write_state(state_path, state)
        return state
    except Exception as exc:
        if state is not None:
            try:
                state['status'] = 'blocked'
                _write_state(state_path, state)
            except Exception:
                pass
        # Only fixed, locally defined messages may cross the transport boundary.
        safe_reasons = {
            'gws request timed out', 'delivery already uses connector', 'gws unavailable', 'gws authentication or validation failed', 'gws request failed',
            'gws returned invalid response', 'Drive listing incomplete', 'unsafe run ID',
            'invalid inventory', 'invalid inventory name', 'local inventory mismatch',
            'delivery state does not match package or destination',
            'delivery transport does not match existing receipt',
            'destination is not a writable folder', 'destination sharing is shared or unknown',
            'multiple remote matches', 'remote ID missing', 'remote readback ID mismatch',
            'remote ID changed', 'remote object not visible; retry later without creating',
            'Drive request rejected before upload', 'remote response lacks ID',
            'remote collision or metadata mismatch', 'remote folder mismatch',
            'remote file size or type mismatch', 'remote checksum mismatch',
            'local package changed during delivery', 'local package or state is not private and regular',
        }
        message = str(exc) if isinstance(exc, DeliveryError) else ''
        reason = message if message in safe_reasons else 'local validation or Drive request failed'
        result = {'status': 'blocked', 'reason': reason}
        if isinstance(exc, TransportFailure):
            result['fallback_transport'] = 'connector'
        return result


def connector_instruction(package_dir, folder_id):
    return ('Delivery: pending; after saving destination metadata as JSON, run connector_export.py next --package-dir '
            + shlex.quote(str(package_dir)) + ' --drive-folder-id ' + shlex.quote(folder_id)
            + ' --destination-metadata DESTINATION_METADATA_JSON')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', required=True)
    parser.add_argument('--drive-folder-id')
    parser.add_argument('--drive-transport', choices=('auto', 'gws', 'connector'))
    parser.add_argument('--allow-shared', action='store_true')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('_delivery_config', Path(__file__).with_name('delivery_config.py'))
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    try:
        delivery = config.resolve_delivery(args.drive_folder_id, args.drive_transport)
    except config.ConfigError as exc:
        print('drive_export.py: ' + str(exc), file=sys.stderr)
        return 2
    if delivery['drive_folder_id'] and delivery['transport'] == 'connector':
        print(connector_instruction(args.package_dir, delivery['drive_folder_id']))
        return 1
    result = upload_bundle(args.package_dir, delivery['drive_folder_id'], allow_shared=args.allow_shared)
    print('Package: ' + args.package_dir)
    if result.get('fallback_transport') == 'connector':
        print(connector_instruction(args.package_dir, delivery['drive_folder_id']))
    elif result['status'] == 'complete':
        print('Delivery: complete' + (': ' + result['folder_link'] if result.get('folder_link') else ''))
    else:
        print('Delivery: ' + result['status'] + ' — ' + result.get('reason', 'delivery failed'))
    return 0 if result['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
