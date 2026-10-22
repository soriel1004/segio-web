#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""On-demand authenticated export worker. Python 2.6 compatible, no root needed."""
from __future__ import print_function
import hashlib
import binascii
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from optparse import OptionParser

from export_approval_db import run_export
from export_schema_from_segio_config import SafeError

PROTOCOL = 'segio-live-archive/1'
CONTROL_PROTOCOL = 'segio-worker-control/1'
MAIL_COPY_REVISION = 'mail-retry-progress-1'
INCREMENTAL_REVISION = 'mail-hash-delta-1'
CANCEL_CLEANUP_REVISION = 'cancel-discard-1'
FAILURE_CODES = set(('db_config_invalid', 'db_config_unreadable', 'psql_unavailable',
    'db_authentication_failed', 'db_permission_denied', 'db_schema_mismatch',
    'db_query_timeout', 'db_database_missing', 'db_connection_failed', 'db_query_failed',
    'db_snapshot_mismatch', 'approval_user_not_found', 'cell_data_missing',
    'db_encoding_failed',
    'mail_path_unconfigured', 'mail_path_missing_or_symlink', 'mail_changed_retry',
    'mail_unsafe_file', 'mail_unsafe_symlink', 'invalid_identity', 'invalid_metadata',
    'unsafe_job', 'cancelled', 'server_disk_full', 'server_file_too_large',
    'server_access_denied', 'server_file_missing', 'export_failed',
    'secure_random_unavailable', 'csrf_required', 'read_only', 'unexpected_parameter',
    'empty_selection', 'invalid_baseline', 'job_storage_failed', 'export_process_unavailable',
    'export_already_running', 'export_busy', 'mail_path_unconfigured'))


def failure_details(exc, stage):
    code = getattr(exc, 'code', None)
    if code not in FAILURE_CODES:
        code = str(exc) if isinstance(exc, SafeError) else None
    number = getattr(exc, 'errno', None)
    if code not in FAILURE_CODES:
        code = {errno.ENOSPC: 'server_disk_full', errno.EFBIG: 'server_file_too_large',
                errno.EACCES: 'server_access_denied', errno.EPERM: 'server_access_denied',
                errno.ENOENT: 'server_file_missing'}.get(number, 'export_failed')
        if isinstance(exc, UnicodeError) and stage == 'database':
            code = 'db_encoding_failed'
    # Never record exception text, paths, SQL, employee IDs or secrets.
    category = ('io' if isinstance(exc, (IOError, OSError)) else
                'validation' if isinstance(exc, SafeError) else
                'encoding' if isinstance(exc, UnicodeError) else 'unexpected')
    diagnostic = {'stage': stage, 'category': category,
                  'errno': number if isinstance(number, int) else None}
    if isinstance(exc, UnicodeError):
        kinds = ('UnicodeEncodeError','UnicodeDecodeError','UnicodeTranslateError','UnicodeError')
        kind = type(exc).__name__
        diagnostic['exception_type'] = kind if kind in kinds else 'UnicodeError'
        codec = getattr(exc,'encoding',None)
        diagnostic['encoding'] = codec if codec in (
            'ascii','utf-8','utf8','cp949','euc-kr','euc_kr','latin-1') else 'unknown'
        trace = sys.exc_info()[2]
        while trace is not None:
            frame = trace.tb_frame
            module = os.path.basename(frame.f_code.co_filename)
            function = frame.f_code.co_name
            if module == 'export_approval_db.py' and function in (
                    'text','read_rows','local_file','image','attachment','freeze',
                    'render_bundle','run_export','decode_display_markup','signature_data_uri'):
                diagnostic['site'] = {'function':function,'line':trace.tb_lineno}
            trace = trace.tb_next
    return code, diagnostic


class MailChanged(SafeError):
    def __init__(self, stage, expected, actual):
        SafeError.__init__(self, 'mail_changed_retry')
        fields = ('device', 'inode', 'size', 'modified', 'changed')
        self.diagnostic = {'stage': stage,
                           'fields': [key for key, a, b in zip(fields, expected, actual) if a != b]}


def write_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.update-', dir=os.path.dirname(path))
    stream = os.fdopen(fd, 'wb')
    try:
        stream.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    finally:
        stream.close()
    # Windows rename does not replace files; the deployed worker uses POSIX.
    replace = getattr(os, 'replace', None)
    if replace:
        replace(temporary, path)
    else:
        if os.name == 'nt' and os.path.exists(path):
            os.unlink(path)
        os.rename(temporary, path)


def read_json(path, limit=65536):
    if os.path.islink(path):
        raise SafeError('unsafe_job')
    with open(path, 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SafeError('invalid_metadata')
    return json.loads(data.decode('utf-8'))


def check_user(user):
    if not re.match(r'\A[A-Za-z0-9_.@-]{1,100}\Z', user) or user in ('.', '..') or user.lower() == 'guest':
        raise SafeError('invalid_identity')


def mail_path(template, user):
    check_user(user)
    if not template or template.count('{user}') != 1 or '{user}' not in template.split('/'):
        raise SafeError('mail_path_unconfigured')
    if template.count('{initial}') > 1 or ('{initial}' in template and '{initial}' not in template.split('/')):
        raise SafeError('mail_path_unconfigured')
    if '{initial}' in template and not user[0].isalnum():
        raise SafeError('invalid_identity')
    path = os.path.abspath(template.replace('{user}', user).replace('{initial}', user[0]))
    # json loads Unicode on Python 2. Walk using native byte paths so CentOS
    # does not implicitly decode legacy CP949 file names using its locale.
    if sys.version_info[0] < 3 and not isinstance(path, bytes):
        path = path.encode(sys.getfilesystemencoding() or 'utf-8')
    if os.path.realpath(path) != path or not os.path.exists(path):
        raise SafeError('mail_path_missing_or_symlink')
    return path


def file_identity(info):
    # Prefer native integer timestamp precision over floating approximations.
    # POSIX ctime records metadata changes. Windows legacy ctime is creation
    # time, with different stat/fstat reporting; it is not a change marker.
    return (info.st_dev, info.st_ino, info.st_size,
            getattr(info, 'st_mtime_ns', info.st_mtime),
            getattr(info, 'st_ctime_ns', info.st_ctime) if os.name != 'nt' else None)


def fingerprint(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise SafeError('mail_unsafe_file')
    return file_identity(info)


def inventory(root):
    if os.path.isfile(root):
        return [(os.path.basename(root), root, fingerprint(root))]
    result = []
    def walk_error(error):
        raise error
    for current, dirs, files in os.walk(root, onerror=walk_error):
        for name in dirs:
            if os.path.islink(os.path.join(current, name)):
                raise SafeError('mail_unsafe_symlink')
        for name in files:
            path = os.path.join(current, name)
            result.append((os.path.relpath(path, root).replace(os.sep, '/'), path, fingerprint(path)))
    return sorted(result)


def copy_mail_file(path, destination, expected, checkpoint, progress):
    """Copy a stable regular file; never accept a changing prefix as complete."""
    digest, count = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    source = os.fdopen(descriptor, 'rb')
    try:
        actual = file_identity(os.fstat(source.fileno()))
        if actual != expected:
            raise MailChanged('open', expected, actual)
        with open(destination, 'wb') as target:
            while True:
                checkpoint()
                block = source.read(min(1024 * 1024, expected[2] - count + 1))
                if not block:
                    break
                count += len(block)
                if count > expected[2]:
                    raise MailChanged('copy', expected, file_identity(os.fstat(source.fileno())))
                target.write(block)
                digest.update(block)
                progress(count)
        actual = fingerprint(path)
        if count != expected[2] or actual != expected:
            raise MailChanged('copy', expected, actual)
        return {'bytes': count, 'sha256': digest.hexdigest()}
    finally:
        source.close()


def parse_mail_baseline(raw):
    if raw is None:
        return None
    try:
        if len(raw.encode('utf-8')) > 48000:
            raise ValueError()
        value=json.loads(raw)
        entries=value['files']
        if value.get('protocol')!='segio-incremental/1' or not isinstance(entries,list) or len(entries)>1000:
            raise ValueError()
        result={}
        for item in entries:
            name=item['name_bytes_hex']
            size=item['bytes']
            if (not re.match(r'\A(?:[a-f0-9]{2}){1,4096}\Z',name) or name in result or
                    isinstance(size,bool) or size<0 or int(size)!=size or
                    not re.match(r'\A[a-f0-9]{64}\Z',item['sha256'])):
                raise ValueError()
            result[name]={'bytes':size,'sha256':item['sha256']}
        return result
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise SafeError('invalid_baseline')


def mail_name_hex(name):
    raw = name if isinstance(name,bytes) else name.encode(sys.getfilesystemencoding(), 'surrogateescape' if sys.version_info[0]>=3 else 'strict')
    return binascii.hexlify(raw).decode('ascii')


def copy_mail(template, user, folder, manifest, checkpoint, progress=lambda **fields: None, baseline=None):
    root = mail_path(template, user)
    cached, slots = {}, {}
    failure = SafeError('mail_changed_retry')
    # Reuse stable files on retry, but reconcile the entire inventory before
    # publishing. New/deleted/renamed files cannot silently disappear.
    for attempt in range(1, 4):
        checkpoint()
        root = mail_path(template, user)
        try:
            before = inventory(root)
        except (IOError, OSError) as exc:
            if exc.errno == errno.ENOENT:
                continue
            raise
        total = sum(item[2][2] for item in before)
        completed, files_done = 0, 0
        progress(completed_bytes=0, total_bytes=total, completed_files=0,
                 total_files=len(before), attempt=attempt)
        for name, path, expected in before:
            checkpoint()
            if name not in slots:
                slots[name] = len(slots)
            relative = 'mail/raw/%06d.bin' % slots[name]
            destination = os.path.join(folder, *relative.split('/'))
            if not os.path.isdir(os.path.dirname(destination)):
                os.makedirs(os.path.dirname(destination), 0o700)
            def copying(count):
                progress(completed_bytes=completed + count, total_bytes=total,
                         completed_files=files_done, total_files=len(before), attempt=attempt)
            if name not in cached or cached[name][0] != expected:
                cached.pop(name, None)
                try:
                    info = copy_mail_file(path, destination, expected, checkpoint, copying)
                    reused=baseline is not None and baseline.get(mail_name_hex(name))==info
                    if reused:
                        os.unlink(destination)
                    cached[name] = (expected, info, reused)
                except MailChanged as exc:
                    failure = exc
                    if os.path.exists(destination):
                        os.unlink(destination)
                    continue
                except (IOError, OSError) as exc:
                    if exc.errno != errno.ENOENT:
                        raise
                    if os.path.exists(destination):
                        os.unlink(destination)
                    continue
            completed += expected[2]
            files_done += 1
            copying(0)
        try:
            after = inventory(root)
        except (IOError, OSError) as exc:
            if exc.errno == errno.ENOENT:
                continue
            raise
        if after == before and all(name in cached and cached[name][0] == expected
                                   for name, path, expected in before):
            break
    else:
        raise failure
    sources = []
    present = set(name for name, path, expected in before)
    for name in slots:
        if name not in present:
            stale = os.path.join(folder, 'mail', 'raw', '%06d.bin' % slots[name])
            if os.path.exists(stale):
                os.unlink(stale)
    for name, path, expected in before:
        relative = 'mail/raw/%06d.bin' % slots[name]
        info = cached[name][1]
        if cached[name][2]:
            manifest['incremental']['reused_mail'][relative]=dict(info,name_bytes_hex=mail_name_hex(name))
        else:
            manifest['files'][relative] = info
        raw_name = name if isinstance(name, bytes) else name.encode(sys.getfilesystemencoding(), 'surrogateescape' if sys.version_info[0]>=3 else 'strict')
        try:
            display_name = raw_name.decode('utf-8')
        except UnicodeError:
            try:
                display_name = raw_name.decode('cp949')
            except UnicodeError:
                display_name = raw_name.decode('utf-8', 'replace')
        sources.append({'name': display_name, 'name_bytes_hex': binascii.hexlify(raw_name).decode('ascii'),
                        'path': relative, 'bytes': info['bytes'], 'sha256': info['sha256']})
    path = os.path.join(folder, 'mail', 'sources.json')
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), 0o700)
    reused=manifest.get('incremental',{}).get('reused_mail',{})
    write_json(path, {'user': user, 'sources': sources, 'raw_files_complete': not bool(reused)})
    with open(path, 'rb') as stream:
        body = stream.read()
    manifest['files']['mail/sources.json'] = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
    manifest['counts']['mail_files'] = len(sources)
    manifest['coverage'].append({'kind': 'mail', 'status': 'raw_files_copied', 'files': len(sources)})


class PackingProgress:
    """Observe actual ZIP output on both Python 2.6 and modern zipfile."""
    def __init__(self, stream, checkpoint, report):
        self.stream, self.checkpoint, self.report = stream, checkpoint, report
        self.high_water = 0

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, data):
        self.checkpoint()
        result = self.stream.write(data)
        self.high_water = max(self.high_water, self.stream.tell())
        self.report(completed_bytes=self.high_water, total_bytes=None)
        return result


def write_archive_file(archive, path, name):
    """Python 2.6 write() omits ZIP64 local headers for large single files."""
    info = os.stat(path)
    if sys.version_info >= (2, 7) or info.st_size <= zipfile.ZIP64_LIMIT:
        return archive.write(path, name)
    # Backport the fixed-size-header strategy. Staged input is immutable, so
    # reserve ZIP64 before streaming and rewrite the complete header afterward.
    item = zipfile.ZipInfo(name, time.localtime(info.st_mtime)[:6])
    item.compress_type = archive.compression
    item.external_attr = (info.st_mode & 0xffff) << 16
    item.header_offset = archive.fp.tell()
    item.file_size, item.compress_size, item.CRC = info.st_size, 0, 0
    archive._writecheck(item)
    archive._didModify = True
    header = item.FileHeader()
    archive.fp.write(header)
    compressor = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
    size, compressed, crc = 0, 0, 0
    with open(path, 'rb') as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            crc = binascii.crc32(block, crc) & 0xffffffff
            data = compressor.compress(block)
            archive.fp.write(data)
            compressed += len(data)
    data = compressor.flush()
    archive.fp.write(data)
    compressed += len(data)
    if size != info.st_size:
        raise SafeError('invalid_metadata')
    item.file_size, item.compress_size, item.CRC = size, compressed, crc
    final_header = item.FileHeader()
    if len(final_header) != len(header):
        raise SafeError('invalid_metadata')
    position = archive.fp.tell()
    archive.fp.seek(item.header_offset)
    archive.fp.write(final_header)
    archive.fp.seek(position)
    archive.filelist.append(item)
    archive.NameToInfo[item.filename] = item


def run_job(config_path, job_id):
    if not re.match(r'\A[a-f0-9]{32}\Z', job_id):
        raise SafeError('invalid_job')
    config = read_json(config_path)
    job = os.path.join(config['root'], 'jobs', job_id)
    if os.path.islink(job) or not os.path.isdir(job):
        raise SafeError('unsafe_job')
    request = read_json(os.path.join(job, 'request.json'))
    user = request['username']
    check_user(user)
    status_file = os.path.join(job, 'status.json')
    def status(state, phase, **extra):
        value = {'protocol': PROTOCOL, 'username': user, 'job': job_id, 'state': state, 'phase': phase}
        value.update(extra)
        write_json(status_file, value)
    def checkpoint():
        if os.path.exists(os.path.join(job, 'cancel')):
            raise SafeError('cancelled')
    last_progress = [None, 0, None]
    def report(phase, **fields):
        now = time.time()
        attempt = fields.get('attempt')
        if phase != last_progress[0] or attempt != last_progress[2] or now - last_progress[1] >= 1:
            status('running', phase, progress=fields)
            last_progress[:] = [phase, now, attempt]
    work = os.path.join(job, 'work')
    stage = 'initializing'
    try:
        os.mkdir(work, 0o700)
        checkpoint()
        baseline=parse_mail_baseline(request.get('mail_baseline'))
        if request.get('approvals') is True:
            stage = 'database'
            status('running', 'database')
            folder, intermediate = run_export(config['db_config'], user, work, cell=config['cell'],
                                               binary=config.get('psql', '/usr/bin/psql'), live_scope=True,checkpoint=checkpoint)
            os.unlink(intermediate)
            manifest = read_json(os.path.join(folder, 'manifest.json'), 16 * 1024 * 1024)
        else:
            folder = os.path.join(work, 'bundle')
            os.mkdir(folder, 0o700)
            manifest = {'format': 'segio-db-html/1', 'server': 'cell_000', 'user': user,
                        'state': 'collected_with_warnings', 'account_deletion_verified': False,
                        'counts': {'approval': 0}, 'files': {}, 'coverage': [],
                        'warnings': [u'계정 삭제 영향은 검증되지 않았습니다.'], 'snapshot_rows': {}}
            for name, body in (('catalog.json', b'{"items":[]}'), ('index.html', b'<meta charset="utf-8"><p>Mail import pending</p>')):
                with open(os.path.join(folder, name), 'wb') as stream:
                    stream.write(body)
                manifest['files'][name] = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        checkpoint()
        if request.get('mail') is True:
            stage = 'mail'
            status('running', 'mail')
            if baseline is not None:
                manifest['incremental']={'protocol':'segio-incremental/1','reused_mail':{},'basis':'content_sha256'}
            copy_mail(config.get('mail_template'), user, folder, manifest, checkpoint,
                      lambda **fields: report('mail', **fields),baseline=baseline)
        manifest['live_export'] = {'requested_at': request['created_at'], 'job': job_id,
                                   'approvals': request.get('approvals') is True, 'mail': request.get('mail') is True}
        manifest['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        write_json(os.path.join(folder, 'manifest.json'), manifest)
        stage = 'packing'
        status('running', 'packing')
        archive_path = os.path.join(job, 'archive.zip')
        archive = zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True)
        archive.fp = PackingProgress(archive.fp, checkpoint,
                                     lambda **fields: report('packing', **fields))
        try:
            for current, dirs, files in os.walk(folder):
                for name in files:
                    checkpoint()
                    path = os.path.join(current, name)
                    write_archive_file(archive, path, os.path.relpath(path, folder).replace(os.sep, '/'))
        finally:
            archive.close()
        checkpoint()
        stage = 'hashing'
        status('running', 'hashing')
        digest = hashlib.sha256()
        hashed, archive_size = 0, os.path.getsize(archive_path)
        with open(archive_path, 'rb') as stream:
            while True:
                checkpoint()
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                hashed += len(block)
                report('hashing', completed_bytes=hashed, total_bytes=archive_size)
        shutil.rmtree(work)
        status('ready', 'ready', version=job_id, bytes=os.path.getsize(archive_path),
               sha256=digest.hexdigest(), counts=manifest['counts'], prepared_at=manifest['finished_at'])
    except Exception as exc:
        code, diagnostic = failure_details(exc, stage)
        if isinstance(exc, MailChanged):
            diagnostic['mail_change'] = exc.diagnostic
        status('failed', code, diagnostic=diagnostic)
        # The client removes finished job folders even on failure. Retain only
        # the latest anonymous error so admins can diagnose after cleanup.
        try:
            write_json(os.path.join(config['root'], 'jobs', 'last-failure.json'),
                       {'protocol': PROTOCOL, 'phase': code, 'diagnostic': diagnostic,
                        'failed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        except (IOError, OSError):
            pass
        archive_path = os.path.join(job, 'archive.zip')
        if os.path.isfile(archive_path):
            os.unlink(archive_path)
    finally:
        if os.path.isdir(work):
            shutil.rmtree(work)
        # Only an authenticated cleanup request creates this marker. Keep an
        # owner receipt so a disconnected client can later confirm deletion.
        if os.path.isfile(os.path.join(job,'discard-on-cancel')):
            receipt=os.path.join(config['root'],'jobs','.'+job_id+'.cleanup.json')
            proof={'protocol':PROTOCOL,'username':user,'job':job_id,
                   'created_at':int(time.time()),'state':'cleanup_requested'}
            try:
                write_json(receipt,proof)
                if os.path.isdir(job) and not os.path.islink(job):
                    shutil.rmtree(job)
                if not os.path.lexists(job):
                    proof['state']='cleaned'
                    write_json(receipt,proof)
            except (IOError,OSError):
                pass


def control_response(operation, **fields):
    value = {'protocol': CONTROL_PROTOCOL, 'operation': operation, 'ok': True}
    value.update(fields)
    print(json.dumps(value, separators=(',', ':')))


def main():
    parser = OptionParser()
    parser.add_option('--config')
    parser.add_option('--job')
    parser.add_option('--detach', action='store_true')
    parser.add_option('--random-token', action='store_true')
    parser.add_option('--bridge-control', action='store_true')
    options, args = parser.parse_args()
    if options.random_token:
        if args or options.config or options.job or options.detach:
            parser.error('--random-token cannot be combined with job arguments')
        try:
            token = binascii.hexlify(os.urandom(16)).decode('ascii')
        except (OSError, NotImplementedError):
            return 1
        if options.bridge_control:
            control_response('random-token', token=token)
        else:
            print(token)
        return 0
    if args or not options.config or not options.job:
        parser.error('--config and --job required')
    if options.bridge_control and not options.detach:
        parser.error('--bridge-control requires --random-token or --detach')
    if options.detach:
        if options.bridge_control and not re.match(r'\A[a-f0-9]{32}\Z', options.job):
            parser.error('Invalid job')
        if hasattr(os, 'fork'):
            if os.fork():
                if options.bridge_control:
                    control_response('detach', job=options.job)
                return 0
            os.setsid()
            if os.fork():
                os._exit(0)
            descriptor = os.open(os.devnull, os.O_RDWR)
            for number in (0, 1, 2):
                os.dup2(descriptor, number)
            if descriptor > 2:
                os.close(descriptor)
        else:
            stream = open(os.devnull, 'r+b')
            try:
                subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0]), '--config', options.config, '--job', options.job],
                                 stdin=stream, stdout=stream, stderr=stream, creationflags=0x00000208)
            finally:
                stream.close()
            if options.bridge_control:
                control_response('detach', job=options.job)
            return 0
    run_job(options.config, options.job)
    return 0


if __name__ == '__main__':
    sys.exit(main())
