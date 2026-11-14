#!/usr/bin/env python3
"""Plan connector operations and verify independent Drive readback, without network access."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile

from bundle import validate_bundle
from delivery_config import resolve_delivery

FOLDER = 'application/vnd.google-apps.folder'


def private(path, directory=False):
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or info.st_mode & 0o077:
        raise ValueError('unsafe local state')


def digest(path):
    private(path)
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return {'size_bytes': path.stat().st_size, 'sha256': h.hexdigest()}


def save(path, state):
    if path.exists() or path.is_symlink():
        private(path)
    fd, name = tempfile.mkstemp(prefix='.delivery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prepare_archive(path, run_id, inventory):
    directory = Path(tempfile.mkdtemp(prefix='.token-audit-', dir=path.parent))
    archive = directory / ('token-audit-' + run_id + '.zip')
    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w+b') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as zipped:
            for name in sorted(inventory):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o600 << 16
                h = hashlib.sha256()
                size = 0
                with (path / name).open('rb') as source, zipped.open(info, 'w') as target:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        target.write(block)
                        h.update(block)
                        size += len(block)
                if {'size_bytes': size, 'sha256': h.hexdigest()} != inventory[name]:
                    raise ValueError('package changed during archive creation')
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return {'path': str(archive), **digest(archive)}


def metadata(path):
    with open(path, encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError('invalid metadata')
    return value


def check(item, ident, name=None, parent=None, folder=True):
    if (not isinstance(ident, str) or not ident or item.get('id') != ident
            or item.get('shared') is not False or item.get('drive_id') is not None
            or item.get('trashed') is True
            or (name is not None and item.get('title') != name)
            or (parent is not None and item.get('parent_ids') != [parent])):
        raise ValueError('remote metadata mismatch')
    mime = item.get('mime_type')
    if not isinstance(mime, str) or (mime != FOLDER if folder else mime not in ('application/zip', 'application/octet-stream', 'application/x-zip-compressed')):
        raise ValueError('remote type mismatch')


def process(args):
    path = Path(args.package_dir).expanduser().absolute()
    private(path, directory=True)
    lock = path / '.delivery.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private(lock)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return locked(args, path)
    finally:
        os.close(fd)


def locked(args, path):
    manifest = validate_bundle(path)
    inventory = {entry['name']: digest(path / entry['name']) for entry in manifest['files']}
    inventory['manifest.json'] = digest(path / 'manifest.json')
    receipt_path = path / 'delivery-receipt.json'
    if receipt_path.exists() or receipt_path.is_symlink():
        private(receipt_path)
        state = json.loads(receipt_path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('transport') not in (None, 'gws', 'connector')
                or state.get('run_id') != manifest['run_id']
                or state.get('manifest_sha256') != inventory['manifest.json']['sha256']
                or (state.get('transport') == 'connector' and state.get('inventory') != inventory)
                or not isinstance(state.get('files'), dict)):
            raise ValueError('state conflict')
    else:
        state = None
    if state is not None and state.get('transport') == 'connector' and state.get('archive') is not None:
        archive = state['archive']
        archive_path = Path(archive['path'])
        private(archive_path.parent, directory=True)
        if digest(archive_path) != {k: archive[k] for k in ('size_bytes', 'sha256')}:
            raise ValueError('archive changed')
    if args.command == 'next':
        config = resolve_delivery(args.drive_folder_id, 'connector')
        destination = config['drive_folder_id']
        if not destination:
            raise ValueError('destination missing')
        check(metadata(args.destination_metadata), destination)
        if state is not None and state.get('transport') in (None, 'gws'):
            if state.get('destination_id') != destination:
                raise ValueError('destination conflict')
            files = state['files']
            folder_id = state.get('folder_id')
            pending = state.get('pending')
            folder_name = 'token-audit-' + manifest['run_id']
            if (state.get('status') not in ('blocked', 'in progress', 'complete')
                    or (folder_id is not None and (not isinstance(folder_id, str) or not folder_id or folder_id == destination))
                    or ('folder_link' in state and not isinstance(state['folder_link'], str))
                    or any(name not in inventory or not isinstance(value, dict)
                           or not isinstance(value.get('id'), str) or not value['id']
                           or not isinstance(value.get('verification'), str) or not value['verification']
                           for name, value in files.items())
                    or (pending is not None and (not isinstance(pending, str) or pending not in set(inventory) | {folder_name}))
                    or (not folder_id and (files or pending not in (None, folder_name)))
                    or (folder_id and pending == folder_name)):
                raise ValueError('invalid gws state')
            if state['status'] == 'complete':
                if not folder_id or pending is not None or set(files) != set(inventory):
                    raise ValueError('invalid completed gws state')
                return {'status': 'complete', 'folder_id': folder_id, 'folder_link': state.get('folder_link')}
            if folder_id or pending == folder_name:
                if not args.folder_metadata:
                    if folder_id:
                        return {'status': 'in progress', 'action': 'read_gws_folder', 'folder_id': folder_id}
                    return {'status': 'in progress', 'action': 'reconcile_gws_folder',
                            'parent_id': destination, 'name': folder_name, 'run_id': manifest['run_id'],
                            'property': {'field': 'properties', 'key': 'tokenAuditRunId', 'value': manifest['run_id']}}
                item = metadata(args.folder_metadata)
                observed_id = folder_id or item.get('id')
                check(item, observed_id, folder_name, destination)
                folder_id = observed_id
            original = state
            state = {'run_id': manifest['run_id'], 'destination_id': destination, 'transport': 'connector',
                     'manifest_sha256': inventory['manifest.json']['sha256'], 'inventory': inventory,
                     'files': {}, 'pending': None, 'status': 'in progress', 'previous_gws_delivery': original}
            if folder_id:
                state['folder_id'] = folder_id
                if item.get('url'):
                    state['folder_link'] = item['url']
        if state is None:
            state = {'run_id': manifest['run_id'], 'destination_id': destination, 'transport': 'connector',
                     'manifest_sha256': inventory['manifest.json']['sha256'], 'inventory': inventory,
                     'files': {}, 'pending': None, 'status': 'in progress'}
        if state.get('destination_id') != destination:
            raise ValueError('destination conflict')
        folder_id = state.get('folder_id')
        folder_name = 'token-audit-' + manifest['run_id']
        if folder_id:
            if not args.folder_metadata:
                raise ValueError('folder metadata required')
            check(metadata(args.folder_metadata), folder_id, folder_name, destination)
        pending = state.get('pending')
        if pending:
            return {'status': 'in progress', 'action': 'reconcile_pending', 'pending': pending}
        if not folder_id:
            pending = {'action': 'create_folder', 'name': folder_name, 'parent_id': destination}
        else:
            if state.get('archive_id'):
                state['status'] = 'complete'
                save(receipt_path, state)
                return {'status': 'complete', 'folder_id': folder_id, 'folder_link': state.get('folder_link')}
            if state.get('archive') is not None:
                raise ValueError('archive state conflict')
            state['archive'] = prepare_archive(path, manifest['run_id'], inventory)
            validate_bundle(path)
            if digest(path / 'manifest.json') != inventory['manifest.json']:
                raise ValueError('package changed during archive creation')
            pending = {'action': 'upload_file', 'name': 'token-audit-' + manifest['run_id'] + '.zip',
                       'parent_id': folder_id, 'local_path': state['archive']['path']}
        state['pending'] = pending
        state['status'] = 'in progress'
        save(receipt_path, state)
        return {'status': 'in progress', **pending}
    if state is None or state.get('transport') != 'connector' or not isinstance(state.get('pending'), dict):
        raise ValueError('no pending operation')
    pending = state['pending']
    kind = pending.get('action')
    if kind not in ('create_folder', 'upload_file'):
        raise ValueError('invalid pending operation')
    item = metadata(args.metadata)
    check(item, args.created_id, pending.get('name'), pending.get('parent_id'), folder=kind == 'create_folder')
    if kind == 'create_folder':
        if pending.get('name') != 'token-audit-' + manifest['run_id'] or pending.get('parent_id') != state.get('destination_id') or state.get('folder_id'):
            raise ValueError('invalid folder state')
        state['folder_id'] = args.created_id
        if item.get('url'):
            state['folder_link'] = item['url']
    else:
        archive = state.get('archive')
        if (not isinstance(archive, dict) or pending.get('name') != 'token-audit-' + manifest['run_id'] + '.zip'
                or pending.get('parent_id') != state.get('folder_id') or state.get('archive_id')
                or pending.get('local_path') != archive.get('path') or not args.downloaded_file):
            raise ValueError('invalid archive state')
        for size_key in ('size', 'size_bytes'):
            if size_key in item and str(item[size_key]) != str(archive['size_bytes']):
                raise ValueError('remote size mismatch')
        downloaded = Path(args.downloaded_file)
        if not downloaded.is_file() or any(os.path.samefile(downloaded, source) for source in
                                             [Path(archive['path']), *(path / name for name in inventory)]):
            raise ValueError('download missing or local source')
        h = hashlib.sha256()
        size = 0
        with downloaded.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block); size += len(block)
        if {'size_bytes': size, 'sha256': h.hexdigest()} != {k: archive[k] for k in ('size_bytes', 'sha256')}:
            raise ValueError('download mismatch')
        if args.created_id in (state['folder_id'], state['destination_id']):
            raise ValueError('duplicate id')
        state['archive_id'] = args.created_id
        if item.get('url'):
            state['archive_link'] = item['url']
        state['files'] = {name: {'id': args.created_id, 'verification': 'sha256', **details,
                                 **({'link': item['url']} if item.get('url') else {})}
                          for name, details in inventory.items()}
    state['pending'] = None
    save(receipt_path, state)
    return {'status': 'in progress'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    nxt = commands.add_parser('next')
    nxt.add_argument('--package-dir', required=True)
    nxt.add_argument('--drive-folder-id')
    nxt.add_argument('--destination-metadata', required=True)
    nxt.add_argument('--folder-metadata')
    record = commands.add_parser('record')
    record.add_argument('--package-dir', required=True)
    record.add_argument('--created-id', required=True)
    record.add_argument('--metadata', required=True)
    record.add_argument('--downloaded-file')
    args = parser.parse_args()
    try:
        result = process(args)
    except Exception:
        result = {'status': 'blocked', 'reason': 'local validation, state, or readback failed'}
    print(json.dumps(result, sort_keys=True))
    return 1 if result['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
