#!/usr/bin/env python3
"""Manage the private, machine-local token audit delivery destination."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile


class ConfigError(Exception):
    pass


def config_path():
    return Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'codex-token-audit.json'


def _validate(folder_id, transport):
    if not isinstance(folder_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', folder_id):
        raise ConfigError('invalid delivery configuration')
    if transport not in ('auto', 'gws', 'connector'):
        raise ConfigError('invalid delivery configuration')


def load_config():
    path = config_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise ConfigError('cannot safely read delivery configuration') from None
    try:
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ConfigError('cannot safely read delivery configuration')
            data = json.load(stream)
        if not isinstance(data, dict) or set(data) != {'schema_version', 'drive_folder_id', 'transport'} or type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise ConfigError('invalid delivery configuration')
        _validate(data['drive_folder_id'], data['transport'])
        return data
    except (OSError, ValueError, UnicodeError, TypeError):
        raise ConfigError('invalid delivery configuration') from None


def resolve_delivery(folder_id=None, transport=None):
    data = load_config()
    folder = folder_id if folder_id is not None else data.get('drive_folder_id')
    selected = transport if transport is not None else data.get('transport', 'auto')
    if folder is not None:
        _validate(folder, selected)
    elif selected not in ('auto', 'gws', 'connector'):
        raise ConfigError('invalid delivery configuration')
    if selected == 'auto':
        selected = 'gws' if shutil.which('gws') else 'connector'
    return {'drive_folder_id': folder, 'transport': selected}


def save_config(folder_id, transport):
    _validate(folder_id, transport)
    path = config_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Refuse to replace an existing unsafe entry, including dangling symlinks.
        if path.exists() or path.is_symlink():
            load_config()
        fd, temporary = tempfile.mkstemp(prefix='.codex-token-audit-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'schema_version': 1, 'drive_folder_id': folder_id, 'transport': transport}, stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path
    except OSError:
        raise ConfigError('cannot safely write delivery configuration') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    configure = commands.add_parser('configure')
    configure.add_argument('--drive-folder-id', required=True)
    configure.add_argument('--transport', default='auto', choices=('auto', 'gws', 'connector'))
    commands.add_parser('show')
    args = parser.parse_args(argv)
    try:
        if args.command == 'configure':
            save_config(args.drive_folder_id, args.transport)
            print('Delivery configuration saved.')
        else:
            print(json.dumps(load_config(), sort_keys=True))
        return 0
    except ConfigError as exc:
        print('delivery_config.py: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
