#!/usr/bin/env python
"""Private administrator SSH helper. Python 2.6+. Never modifies source records."""
from __future__ import print_function
import binascii
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from optparse import OptionParser

from export_approval_db import run_export
from export_schema_from_segio_config import SafeError
from live_export_worker import (read_json, write_json, check_user, mail_path, inventory,
                                fingerprint, file_identity, failure_details)

PROTOCOL = 'segio-ssh-archive/1'


def source_hex(path):
    if not isinstance(path, bytes):
        path = path.encode(sys.getfilesystemencoding() or 'utf-8', 'surrogateescape' if sys.version_info[0]>=3 else 'strict')
    return binascii.hexlify(path).decode('ascii')


def hash_file(path, expected=None, progress=lambda count: None):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    digest, size = hashlib.sha256(), 0
    with os.fdopen(descriptor, 'rb') as stream:
        before = file_identity(os.fstat(stream.fileno()))
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or (expected and before != expected):
            raise SafeError('mail_changed_retry')
        while True:
            block = stream.read(min(1024 * 1024, before[2]-size+1))
            if not block:
                break
            digest.update(block)
            size += len(block)
            if size>before[2]:
                raise SafeError('mail_changed_retry')
            progress(size)
    if fingerprint(path) != before or size != before[2]:
        raise SafeError('mail_changed_retry')
    return {'bytes': size, 'sha256': digest.hexdigest()}


def mail_snapshot(request, status):
    """Retry changing inventories without copying raw MBOX or rehashing stable files."""
    cache={}
    for attempt in range(1,4):
        try:
            mailbox_root=mail_path(request['mail_template'],request['user'])
            before=inventory(mailbox_root)
            sources,transfers,identities=[],[],[]
            completed,total=0,sum(item[2][2] for item in before)
            last_report=[0]
            def report(count,force=False):
                now=time.time()
                if force or now-last_report[0]>=1:
                    status('running',attempt=attempt,files_done=len(sources),files_total=len(before),
                           completed_bytes=completed+count,total_bytes=total)
                    last_report[0]=now
            report(0,True)
            for number,(name,path,identity) in enumerate(before):
                cached=cache.get(name)
                if cached and cached[0]==identity:
                    details,kind=cached[1:]
                else:
                    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                    with os.fdopen(descriptor,'rb') as stream:
                        prefix=stream.read(8192)
                    is_mbox=not prefix or prefix.startswith(b'From ')
                    is_eml=(not is_mbox and re.match(b'[A-Za-z][A-Za-z0-9-]*:',prefix) and
                        re.search(b'(?im)^(subject|from|to|date|received|delivered-to|mime-version):',prefix))
                    kind='mbox' if is_mbox else 'rfc822' if is_eml else 'unrecognized'
                    details=hash_file(path,identity,report)
                    cache[name]=(identity,details,kind)
                relative='mail/raw/%06d.%s' % (number,'mbox' if kind=='mbox' else 'eml' if kind=='rfc822' else 'bin')
                raw_name=binascii.unhexlify(source_hex(name))
                try:
                    display=raw_name.decode('utf-8')
                except UnicodeError:
                    display=raw_name.decode('cp949','replace')
                source=dict(details,path=relative,name=display,name_bytes_hex=source_hex(name),format=kind)
                sources.append(source)
                transfers.append(dict(details,path=relative,source_hex=source_hex(path)))
                identities.append({'name_hex':source['name_bytes_hex'],'identity':list(identity)})
                completed+=details['bytes']
                report(0,True)
            if inventory(mailbox_root)==before:
                return sources,transfers,identities
        except SafeError as exc:
            if str(exc)!='mail_changed_retry' and exc.code!='mail_changed_retry':
                raise
        except (IOError,OSError) as exc:
            if exc.errno!=errno.ENOENT:
                raise
    raise SafeError('mail_changed_retry')


def private_root(root):
    root = os.path.abspath(root)
    info = os.lstat(root)
    if (os.path.realpath(root) != root or not stat.S_ISDIR(info.st_mode) or
            not re.match(r'\A\.segio-ssh-[0-9a-f]{32}\Z', os.path.basename(root)) or
            (hasattr(os, 'geteuid') and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
        raise SafeError('unsafe_job')
    request = read_json(os.path.join(root, 'request.json'))
    if request.get('protocol') != PROTOCOL:
        raise SafeError('unsafe_job')
    check_user(request['user'])
    return root, request


def prepare(root):
    root, request = private_root(root)
    status_path = os.path.join(root, 'status.json')
    stage = 'initializing'
    def status(state, **fields):
        value = {'protocol': PROTOCOL, 'state': state, 'stage': stage}
        value.update(fields)
        write_json(status_path, value)
    try:
        user = request['user']
        if request['approvals']:
            stage = 'database'
            status('running')
            folder, archive = run_export(request['db_config'], user, root, cell=request['cell'],
                                         binary=request['psql'], live_scope=True, pack=False)
            if archive:
                os.unlink(archive)
            manifest = read_json(os.path.join(folder, 'manifest.json'), None)
        else:
            folder = os.path.join(root, 'bundle')
            os.mkdir(folder, 0o700)
            with open(os.path.join(folder, 'catalog.json'), 'wb') as stream:
                stream.write(b'{"items":[]}')
            with open(os.path.join(folder, 'index.html'), 'wb') as stream:
                stream.write(b'<meta charset="utf-8"><p>Index pending</p>')
            manifest = {'format': 'segio-db-html/1', 'user': user, 'state': 'collected_with_warnings',
                'account_deletion_verified': False, 'files': {}, 'counts': {'approval': 0},
                'coverage': [], 'warnings': [], 'snapshot_rows': {}}
            for name in ('catalog.json', 'index.html'):
                manifest['files'][name] = hash_file(os.path.join(folder, name))
        transfers, sources = [], []
        if request['mail']:
            stage = 'mail'
            status('running', files_done=0)
            sources,transfers,identities=mail_snapshot(request,status)
            for source in sources:
                manifest['files'][source['path']]=dict((key,source[key]) for key in ('bytes','sha256'))
            write_json(os.path.join(root, 'mail-check.json'), identities)
            mail_dir = os.path.join(folder, 'mail')
            os.mkdir(mail_dir, 0o700)
            source_file = os.path.join(mail_dir, 'sources.json')
            write_json(source_file, {'user': user, 'sources': sources, 'raw_files_complete': True})
            manifest['files']['mail/sources.json'] = hash_file(source_file)
        manifest['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        manifest['ssh_export'] = {'protocol': PROTOCOL, 'job': os.path.basename(root),
            'approvals': request['approvals'], 'mail': request['mail'], 'authentication': 'administrator_ssh'}
        manifest['warnings'].append('Export requested by SSH administrator; employee password was not verified.')
        manifest['counts']['mail_files'] = len(sources)
        manifest['coverage'].append({'kind': 'mail', 'status': 'direct_source_transfer', 'files': len(sources)})
        manifest_path = os.path.join(folder, 'manifest.json')
        write_json(manifest_path, manifest)
        for name in sorted(manifest['files']):
            if name.startswith('mail/raw/'):
                continue
            transfers.append(dict(manifest['files'][name], path=name,
                                  source_hex=source_hex(os.path.join(folder, *name.split('/')))))
        transfers.append(dict(hash_file(manifest_path), path='manifest.json', source_hex=source_hex(manifest_path)))
        write_json(os.path.join(root, 'transfer.json'), {'protocol': PROTOCOL, 'user': user,
                   'job': os.path.basename(root), 'files': transfers})
        status('ready', files=len(transfers), bytes=sum(item['bytes'] for item in transfers))
    except Exception as exc:
        code, diagnostic = failure_details(exc, stage)
        status('failed', code=code, diagnostic=diagnostic)


def verify_sources(root):
    root, request = private_root(root)
    if read_json(os.path.join(root, 'status.json')).get('state') != 'ready':
        raise SafeError('export_not_ready')
    if request['mail']:
        before = read_json(os.path.join(root, 'mail-check.json'), 64 * 1024 * 1024)
        actual = []
        for name, path, identity in inventory(mail_path(request['mail_template'], request['user'])):
            raw_name = name if isinstance(name, bytes) else name.encode(sys.getfilesystemencoding(), 'surrogateescape')
            actual.append({'name_hex': binascii.hexlify(raw_name).decode('ascii'), 'identity': list(identity)})
        if actual != before:
            raise SafeError('mail_changed_retry')
    write_json(os.path.join(root, 'verified.json'), {'protocol': PROTOCOL, 'ok': True})


def main():
    parser = OptionParser()
    parser.add_option('--root')
    parser.add_option('--action')
    parser.add_option('--detach', action='store_true')
    options, args = parser.parse_args()
    if args or not options.root or options.action not in ('prepare', 'verify', 'cleanup'):
        parser.error('Use fixed SSH helper actions only.')
    try:
        root, request = private_root(options.root)
        if options.action == 'prepare':
            if options.detach:
                if os.fork():
                    return 0
                os.setsid()
                if os.fork():
                    os._exit(0)
                descriptor = os.open(os.devnull, os.O_RDWR)
                for number in (0, 1, 2):
                    os.dup2(descriptor, number)
                if descriptor > 2:
                    os.close(descriptor)
            prepare(root)
        elif options.action == 'verify':
            verify_sources(root)
        else:
            if read_json(os.path.join(root, 'verified.json')).get('ok') is not True:
                raise SafeError('unsafe_job')
            shutil.rmtree(root)
        if not options.detach:
            print(json.dumps({'protocol': PROTOCOL, 'ok': True}))
        return 0
    except Exception as exc:
        code, diagnostic = failure_details(exc, 'initializing')
        print(json.dumps({'protocol': PROTOCOL, 'error': code, 'diagnostic': diagnostic}))
        return 1


if __name__ == '__main__':
    os.umask(0o077)
    sys.exit(main())
