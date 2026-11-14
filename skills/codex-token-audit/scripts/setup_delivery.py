#!/usr/bin/env python3
"""Check installation delivery using synthetic data and existing exporter receipts."""
import argparse
import json
from pathlib import Path
import uuid

from bundle import validate_bundle, write_bundle
from connector_export import digest, private
from delivery_config import ConfigError, load_config, resolve_delivery


def probe(output_dir):
    report = {
        'scope': {'session_id': 'synthetic-install-check', 'turn_id': 'synthetic',
                  'windows': [{'turn_id': 'synthetic'}]},
        'coverage': {'status': 'complete', 'warnings': []},
        'provenance': {'source': 'synthetic installation check; no session data'},
        'task_label': 'Synthetic installation delivery check',
        'requests': [], 'events': [], 'compaction_markers': [],
        'reconciliation': {'status': 'not_comparable'},
    }
    path = Path(output_dir).expanduser().absolute()
    rates = Path(__file__).resolve().parents[1] / 'references/standard-short-2026-10-28.json'
    write_bundle(report, path, rates_path=rates,
                 run_id='synthetic-install-' + uuid.uuid4().hex)
    validate_bundle(path)
    return path


def verify(package_dir, destination):
    path = Path(package_dir).expanduser().absolute()
    manifest = validate_bundle(path)
    if (manifest['scope'].get('session_id') != 'synthetic-install-check'
            or not manifest['run_id'].startswith('synthetic-install-')
            or (path / 'requests.jsonl').stat().st_size
            or (path / 'events.jsonl').stat().st_size):
        raise ValueError('not an installation probe')
    receipt_path = path / 'delivery-receipt.json'
    private(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    expected = {item['name']: digest(path / item['name']) for item in manifest['files']}
    expected['manifest.json'] = digest(path / 'manifest.json')
    if (not isinstance(receipt, dict) or receipt.get('status') != 'complete'
            or receipt.get('pending') is not None
            or receipt.get('destination_id') != destination
            or receipt.get('run_id') != manifest['run_id']
            or receipt.get('manifest_sha256') != expected['manifest.json']['sha256']
            or not isinstance(receipt.get('folder_id'), str) or not receipt['folder_id']
            or receipt['folder_id'] == destination
            or not isinstance(receipt.get('files'), dict)
            or set(receipt['files']) != set(expected)
            or receipt.get('transport') not in (None, 'gws', 'connector')):
        raise ValueError('delivery proof incomplete or mismatched')
    for entry in receipt['files'].values():
        if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str)
                or not entry['id'] or entry.get('verification') not in ('sha256', 'md5')):
            raise ValueError('delivery proof lacks independent byte verification')
    if receipt.get('transport') == 'connector':
        if receipt.get('inventory') != expected or not receipt.get('archive_id'):
            raise ValueError('archive proof mismatched')
        for name, entry in receipt['files'].items():
            if (entry.get('id') != receipt['archive_id']
                    or entry.get('sha256') != expected[name]['sha256']
                    or entry.get('size_bytes') != expected[name]['size_bytes']):
                raise ValueError('archive inventory mismatched')
    return {'status': 'ready', 'drive_folder_id': destination,
            'transport': receipt.get('transport') or 'gws',
            'run_id': manifest['run_id'], 'folder_id': receipt['folder_id']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('probe').add_argument('--output-dir', required=True)
    commands.add_parser('verify').add_argument('--package-dir', required=True)
    args = parser.parse_args(argv)
    try:
        if not load_config():
            result, code = {'status': 'destination_required'}, 2
        else:
            config = resolve_delivery()
            result = {'status': 'delivery_check_required', **config}
            code = 2
            if args.command == 'probe':
                result['package_dir'] = str(probe(args.output_dir))
                code = 0
            elif args.command == 'verify':
                result = verify(args.package_dir, config['drive_folder_id'])
                code = 0
    except (OSError, ValueError, TypeError, KeyError, ConfigError):
        result, code = {'status': 'blocked',
                        'reason': 'configuration, probe, or verified delivery is incomplete or unsafe'}, 2
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
