#!/usr/bin/env python
"""Read-only check of the common SSH / employee-login bundle. Python 2.6+."""
from __future__ import print_function
import json
import os
import sys
from optparse import OptionParser

# Keep the SSH wire protocol and upload list compatible with existing EXEs.
SSH_FILES = ('remote_ssh_export.py', 'live_export_worker.py', 'export_approval_db.py',
             'export_schema_from_segio_config.py', 'approval_db_columns.json',
             'db_archive_index.html')
WEB_CODE_FILES = ('live_export_worker.py', 'export_approval_db.py',
                  'export_schema_from_segio_config.py', 'approval_db_columns.json',
                  'db_archive_index.html', 'diagnose_live_archive.py')
REQUIRED_FILES = tuple(sorted(set(SSH_FILES + WEB_CODE_FILES + (
    'install_live_archive.py', 'prepare_login_archive.py', 'live_archive_bridge.php',
    'check_shared_tools.py'))))


class BundleError(Exception):
    pass


def load_bundle(directory):
    """Read everything before installation; never inspect server credentials."""
    contents = {}
    for name in REQUIRED_FILES:
        path = os.path.join(directory, name)
        if os.path.islink(path) or not os.path.isfile(path):
            raise BundleError('Missing or unsafe shared tool: ' + name)
        try:
            with open(path, 'rb') as stream:
                body = stream.read()
            if not body.strip():
                raise ValueError()
            if name.endswith('.py'):
                compile(body, name, 'exec')
            elif name.endswith('.json'):
                json.loads(body.decode('utf-8'))
        except (IOError, OSError, ValueError, SyntaxError):
            raise BundleError('Unreadable or invalid shared tool: ' + name)
        contents[name] = body
    for name, protocol in (('remote_ssh_export.py', b'segio-ssh-archive/1'),
                           ('live_export_worker.py', b'segio-live-archive/1'),
                           ('live_archive_bridge.php', b'segio-live-archive/1'),
                           ('live_export_worker.py', b'segio-worker-control/1'),
                           ('live_archive_bridge.php', b'segio-worker-control/1')):
        if protocol not in contents[name]:
            raise BundleError('Unsupported shared tool protocol: ' + name)
    if b'__PRIVATE_ROOT__' not in contents['live_archive_bridge.php']:
        raise BundleError('Use the original live_archive_bridge.php template.')
    if b'db-native-paths-1' not in contents['export_approval_db.py']:
        raise BundleError('Unsupported approval exporter revision.')
    if b'mail-retry-progress-1' not in contents['live_export_worker.py']:
        raise BundleError('Unsupported mail copy revision.')
    return contents


def main():
    parser = OptionParser(usage='%prog [--directory /path/to/tools]')
    parser.add_option('--directory', default=os.path.dirname(os.path.abspath(__file__)))
    options, args = parser.parse_args()
    if args:
        parser.error('Unexpected arguments')
    try:
        contents = load_bundle(options.directory)
    except BundleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print('Shared tools OK: %d files; SSH archive /1 and employee login /1.' % len(contents))
    print('Worker control protocol: segio-worker-control/1; PHP bridge revision: php-runtime-3.')
    print('Approval exporter revision: db-native-paths-1.')
    print('Mail copy revision: mail-retry-progress-1.')
    print('No server configuration, credentials, permissions or jobs changed.')
    print('This is a local bundle check, not a server login or DB connectivity test.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
