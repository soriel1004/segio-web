#!/usr/bin/env python
"""Install live DB/MBOX export once; never prepares an employee archive. Python 2.6+."""
from __future__ import print_function
import binascii
import json
import os
import pwd
import sys
from optparse import OptionParser

from export_schema_from_segio_config import SafeError, discover_config
from prepare_login_archive import atomic_file, private_directory
from check_shared_tools import BundleError, SSH_FILES, WEB_CODE_FILES, load_bundle

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = tuple(sorted(set(SSH_FILES + WEB_CODE_FILES)))


def install(web_user, config, root=None, mail_template=None):
    if os.geteuid() != 0:
        raise SafeError('Run installer as root, not the export worker.')
    # Read and validate every required source file before changing installation.
    try:
        bundle = load_bundle(HERE)
    except BundleError as exc:
        raise SafeError(str(exc))
    account = pwd.getpwnam(web_user)
    if account.pw_uid == 0:
        raise SafeError('Root web services are not supported.')
    config = os.path.realpath(config)
    cell = os.path.dirname(os.path.dirname(config))
    site = os.path.join(cell, 'folder/html/segio/archive')
    if not os.path.isdir(os.path.dirname(site)) or os.path.islink(site):
        raise SafeError('Confirm the real SEGIO cell web directory.')
    # The native SEGIO PHP runtime can restrict access outside its cell.
    # Keep private files in etc, never inside folder/html or the mail tree.
    root = os.path.abspath(root or os.path.join(cell, 'etc', 'segio-live-archive'))
    if os.path.realpath(root) != root or root.startswith(os.path.join(cell, 'folder') + os.sep):
        raise SafeError('Private installation must be outside the webroot without symlinks.')
    # Fixed PHP single-quoted literal; CLI-configured paths cannot add code.
    literal = root.replace('\\', '\\\\').replace("'", "\\'")
    path = os.path.join(root, 'config.json')
    existing = None
    if os.path.exists(root):
        try:
            with open(path, 'rb') as stream:
                existing = json.loads(stream.read().decode('utf-8'))
            if existing.get('protocol') != 'segio-live-archive/1' or existing.get('cell') != cell or existing.get('web_user') != web_user:
                raise ValueError()
        except (IOError, ValueError):
            raise SafeError('Unknown existing private installation; preserved.')
    endpoint = os.path.join(site, 'live.php')
    if os.path.lexists(endpoint):
        if os.path.islink(endpoint):
            raise SafeError('Existing endpoint symlink refused.')
        with open(endpoint, 'rb') as stream:
            if b'/* SEGIO on-demand archive bridge' not in stream.read(200):
                raise SafeError('Unknown existing live.php; preserved.')
    private_directory(root, account.pw_gid)
    code = os.path.join(root, 'code')
    private_directory(code, account.pw_gid)
    jobs = os.path.join(root, 'jobs')
    if os.path.islink(jobs):
        raise SafeError('Unsafe jobs directory.')
    if not os.path.isdir(jobs):
        os.mkdir(jobs, 0o700)
    os.chown(jobs, account.pw_uid, account.pw_gid)
    os.chmod(jobs, 0o700)
    for name in FILES:
        atomic_file(os.path.join(code, name), bundle[name], account.pw_gid)
    settings = {'protocol': 'segio-live-archive/1', 'root': root, 'cell': cell, 'db_config': config,
                'web_user': web_user, 'python': sys.executable, 'worker': os.path.join(code, 'live_export_worker.py'),
                'mail_template': mail_template or (existing or {}).get('mail_template') or os.path.join(cell, 'home/{initial}/{user}/mbox'),
                'csrf_secret': existing['csrf_secret'] if existing else binascii.hexlify(os.urandom(32)).decode('ascii')}
    # Preserve supported local runtime overrides when refreshing shared code.
    if existing and 'psql' in existing:
        settings['psql'] = existing['psql']
    atomic_file(path, json.dumps(settings).encode('utf-8'), account.pw_gid)
    if not os.path.isdir(site):
        os.mkdir(site, 0o755)
        os.chmod(site, 0o755)
    body = bundle['live_archive_bridge.php'].replace(b'__PRIVATE_ROOT__', literal.encode('utf-8'))
    atomic_file(endpoint, body, account.pw_gid, 0o644)
    print('Installed live employee DB/MBOX export: /segio/archive/live.php')
    print('PHP bridge revision: php-runtime-3; worker control: segio-worker-control/1.')
    print('Approval exporter revision: db-native-paths-1.')
    print('Mail copy revision: mail-retry-progress-1.')
    print('Private configuration: ' + path)
    print('Mail template: ' + settings['mail_template'])
    print('No employee archive was prepared. Windows login starts a fresh job.')
    print('Worker uses the existing web OS account; it must already read DB config, data and mail.')
    print('No DB password, authentication setting or source file permissions were changed.')
    print('Shared SSH helper included; existing SSH clients still use their own private /tmp jobs.')


def main():
    parser = OptionParser(usage='%prog --web-user segio [--config /cell/etc/db.conf]')
    parser.add_option('--web-user')
    parser.add_option('--config')
    parser.add_option('--private-root', help='Private directory; default: cell/etc/segio-live-archive')
    parser.add_option('--mail-template')
    options, args = parser.parse_args()
    if args or not options.web_user:
        parser.error('--web-user is required')
    try:
        install(options.web_user, options.config or discover_config(), options.private_root, options.mail_template)
        return 0
    except SafeError as exc:
        print(str(exc), file=sys.stderr)
    except Exception as exc:
        print('Installation failed (%s); sensitive details omitted.' % type(exc).__name__, file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
