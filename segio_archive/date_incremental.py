"""Date-selected mail deltas, staged beside and committed to an existing backup."""
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .archive import atomic_write
from .backup_folders import (require_backup_file, BaselineValidationError,
                             read_backup_metadata, validate_baseline_manifest)
from .file_ops import merge_io, replace_file, remove_file, retry_file_io, file_diagnostic
from .compact_mail import digest_file
from .incremental import Baseline
from .storage import require_space
from .web import CollectionError

PROTOCOL = 'segio-mail-date/1'
REVISION = 'file-time-merge-1'
APPROVAL_REVISION = 'draft-time-merge-1'
APPROVAL_PROTOCOL = 'segio-approval-date/1'
LOOKBACK_DAYS = 15
LOOKBACK_SECONDS = LOOKBACK_DAYS * 24 * 60 * 60


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


@contextmanager
def update_lock(root):
    path = root.parent/('.segio-update-'+root.name+'.lock')
    if path.is_symlink():
        raise CollectionError('백업 반영 잠금 파일의 경로를 확인하세요.')
    with path.open('a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                if path.stat().st_size == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            error=BaselineValidationError('backup_locked')
            error.filesystem_diagnostic=file_diagnostic(exc)
            raise error from None
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


class DateBaseline(Baseline):
    def __init__(self, root, user, checkpoint=lambda: None):
        root = Path(root).expanduser().resolve()
        if not (root/'manifest.json').exists() and not self.journal(root).exists():
            require_backup_file(root,'manifest.json')
        with update_lock(root):
            self.recover(root)
        self.root, self.entries, self.sources = root, {}, []
        reason='manifest_invalid'
        try:
            path = require_backup_file(root,'manifest.json')
            with merge_io('read_previous','manifest.json'):
                self.manifest,self.original_manifest_signature=read_backup_metadata(path,checkpoint)
            validate_baseline_manifest(self.manifest,user)
            if (root/'mail/sources.json').exists() or 'mail/sources.json' in self.manifest['files']:
                self.load_mail_sources(self.manifest,user,checkpoint)
            # Mail-only updates must not advance the approval snapshot time.
            # Legacy mail-only results lack that time: establish it with a
            # full approval export once instead of guessing a newer cutoff.
            reason='time_unavailable'
            approval_time=self.manifest.get('approval_checkpoint')
            if approval_time is None and self.manifest['live_export'].get('approvals') is True:
                approval_time=self.manifest['live_export'].get('requested_at')
            self.approval_checkpoint=None
            self.approval_since=None
            if approval_time is not None:
                if type(approval_time) is not int or approval_time<1:
                    raise ValueError()
                self.approval_checkpoint=approval_time
                self.approval_since=max(1,approval_time-LOOKBACK_SECONDS)
            value = self.manifest.get('mail_checkpoint', self.manifest['live_export'].get('requested_at'))
            if value is None:
                value = datetime.fromisoformat(self.manifest['finished_at'].replace('Z', '+00:00')).timestamp()
            self.mail_checkpoint = int(value)
            if isinstance(value, bool) or self.mail_checkpoint < 1:
                raise ValueError()
            # Preserve snapshot clocks separately from the overlapping request
            # range. Repeated or single-kind updates must never subtract the
            # lookback from a clock that has already been shifted.
            self.since = max(1,self.mail_checkpoint-LOOKBACK_SECONDS)
            for name in ('catalog.sqlite3', 'catalog.json'):
                reason='database_changed' if name=='catalog.sqlite3' else 'catalog_changed'
                require_backup_file(root,name)
                with merge_io('read_previous',name):
                    if digest_file(self.raw_path(name), checkpoint) != self.manifest['files'][name]:
                        raise BaselineValidationError(reason)
            reason='mail_list_changed'
            if self.manifest.get('viewer') != 'sqlite-mail/1':
                raise BaselineValidationError('viewer_not_ready')
            from .portable_mail import VERSION
            if self.manifest.get('portable_mail',{}).get('version') != VERSION:
                raise BaselineValidationError('portable_not_ready')
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise BaselineValidationError(reason) from None

    @staticmethod
    def journal(root):
        return root.parent/('.segio-update-'+root.name+'.json')

    @classmethod
    def recover(cls, root):
        """Recover a stopped directory exchange without deleting either copy."""
        journal = cls.journal(root)
        if not journal.exists():
            return
        try:
            if journal.is_symlink() or journal.stat().st_size > 4096:
                raise ValueError()
            value = json.loads(journal.read_bytes())
            previous = value['previous']
            if value.get('target') != root.name or not re.fullmatch(r'\.segio-previous-[a-f0-9]{32}', previous):
                raise ValueError()
            old = root.parent/previous
            if old.is_symlink():
                raise ValueError()
            if not root.exists() and old.is_dir():
                os.replace(old, root)
            if not root.is_dir():
                raise ValueError()
            # A completed exchange may leave the old copy after power loss.
            # Preserve it for review rather than guessing it can be removed.
            journal.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            raise BaselineValidationError('interrupted_update') from None

    def validate_delta(self, destination):
        manifest = json.loads((destination/'manifest.json').read_bytes())
        delta = manifest.get('mail_delta', {})
        if (delta.get('protocol') != PROTOCOL or delta.get('revision') != REVISION or
                delta.get('since') != self.since or type(manifest.get('mail_checkpoint')) is not int or
                manifest['mail_checkpoint'] < self.mail_checkpoint):
            raise CollectionError('서버가 요청한 백업 기준 시각과 일치하는 추가 메일을 반환하지 않았습니다. 서버 공용 tools를 갱신하세요.')
        source_path = destination/'mail/sources.json'
        sources,_ = read_backup_metadata(source_path,kind='mail_sources')
        names, paths = set(), set()
        if sources.get('user') != self.manifest['user'] or sources.get('raw_files_complete') is False:
            raise CollectionError('추가 메일의 사용자 또는 원본 목록이 일치하지 않습니다.')
        for item in sources['sources']:
            name, path = item.get('name_bytes_hex',''), item.get('path','')
            expected = {key:item.get(key) for key in ('bytes','sha256')}
            if (not re.fullmatch(r'(?:[a-f0-9]{2}){1,4096}',name) or name in names or path in paths or
                    not re.fullmatch(r'mail/raw/[0-9]{6}\.bin',path) or
                    manifest['files'].get(path) != expected):
                raise CollectionError('추가 메일 원본의 이름·경로·검증 목록이 올바르지 않습니다.')
            names.add(name)
            paths.add(path)
        if paths != {name for name in manifest['files'] if name.startswith('mail/raw/')}:
            raise CollectionError('추가 메일 파일과 원본 목록이 일치하지 않습니다.')

    def validate_approval_delta(self, manifest):
        checkpoint=manifest.get('approval_checkpoint')
        if type(checkpoint) is not int or checkpoint<1:
            raise CollectionError('서버가 결재 백업 기준 시각을 반환하지 않았습니다. 서버 공용 tools를 갱신하세요.')
        delta=manifest.get('approval_delta')
        if self.approval_since is None:
            if delta:
                raise CollectionError('첫 결재 백업에 날짜 제한 결과가 반환되어 중단했습니다.')
        elif (not isinstance(delta,dict) or delta.get('protocol')!=APPROVAL_PROTOCOL or
                delta.get('revision')!=APPROVAL_REVISION or delta.get('basis')!='document_wdate' or
                delta.get('since')!=self.approval_since or checkpoint<self.approval_checkpoint):
            raise CollectionError('서버가 요청한 기안일 기준과 일치하는 추가 결재를 반환하지 않았습니다. 서버 공용 tools를 갱신하세요.')

    def prepare(self, destination, checkpoint, report, log, *, approvals=True, mail=True):
        """Keep old raw/pages immutable; only private metadata is rewritten."""
        from .db_client import safe_member
        from .attachment_safety import attachment_io, attachment_name, AttachmentUnavailable
        destination = Path(destination)
        current = json.loads((destination/'manifest.json').read_bytes())
        incoming_sources = (json.loads((destination/'mail/sources.json').read_bytes())['sources'] if mail else [])
        delta_raw = destination/'.incoming-mail'
        if (destination/'mail/raw').exists():
            # A Windows scanner can hold the newly extracted directory open.
            # Keep that directory in place and move only the received files.
            with merge_io('stage_incoming_mail','mail/raw/'):
                delta_raw.mkdir(mode=0o700)
                for item in incoming_sources:
                    checkpoint()
                    replace_file(destination/item['path'],delta_raw/Path(item['path']).name,checkpoint)
        old_catalog = json.loads((self.root/'catalog.json').read_bytes())
        new_catalog = json.loads((destination/'catalog.json').read_bytes())
        catalog = {item['id']:item for item in old_catalog['items']}
        if approvals:
            catalog.update((item['id'],item) for item in new_catalog['items'])
        old_files, new_files = self.manifest['files'], dict(current['files'])
        incoming_by_name={item['name_bytes_hex']:item for item in incoming_sources}
        replacing_raw={item['path'] for item in self.sources
                       if item['name_bytes_hex'] in incoming_by_name and
                       any(item[key]!=incoming_by_name[item['name_bytes_hex']][key] for key in ('bytes','sha256'))}
        # Do not overwrite an inode shared with the old archive. Only immutable
        # raw mail and completed mail pages/attachments use hard links. All
        # writers replace those paths rather than truncating existing files.
        require_space(destination, 64*1024*1024, '기존 백업 반영 준비')
        total = len(old_files)
        report('merge', 0, total, unit='files')
        for number, (name, expected) in enumerate(old_files.items(), 1):
            checkpoint()
            safe_member(name)
            if name in new_files and not name.startswith('mail/raw/'):
                continue
            if name in replacing_raw:
                # A verified new version is already present. Do not create an
                # old hard link only to detach it again during replacement.
                continue
            source = self.raw_path(name)
            try:
                with merge_io('inspect_previous',name):
                    actual=attachment_io(lambda:retry_file_io(source.stat,checkpoint),name,missing=True)
                if actual.st_size != expected['bytes']:
                    if attachment_name(name):continue
                    raise CollectionError('기존 백업 파일의 크기가 변경되었습니다. 파일 무결성을 검사하세요.')
            except AttachmentUnavailable:
                continue  # The renderer records the absent attachment, never restores it.
            target = destination/name
            with merge_io('create_work_folder',name):
                target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise CollectionError('기존 백업 반영 경로가 중복됩니다.')
            readonly=bool(getattr(actual,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_READONLY',1)) or not (actual.st_mode&stat.S_IWUSR)
            if name.startswith(('mail/raw/', 'mail/offline/')) and not readonly:
                try:
                    with merge_io('link_previous',name):
                        attachment_io(lambda:retry_file_io(lambda:os.link(source,target),checkpoint),name,missing=True)
                except AttachmentUnavailable:
                    continue
                except OSError as exc:
                    if getattr(exc,'winerror',None) in (225,226):
                        raise
                    if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EACCES):
                        raise
                    try:
                        attachment_io(lambda:self.copy(source,target,expected,checkpoint,report,relative=name),name,missing=True)
                    except AttachmentUnavailable:
                        continue
            else:
                try:
                    attachment_io(lambda:self.copy(source,target,expected,checkpoint,report,relative=name),name,missing=True)
                except AttachmentUnavailable:
                    continue
            report('merge', number, total, unit='files')
        files = dict(old_files)
        files.update({name:info for name,info in new_files.items() if not name.startswith('mail/raw/')})
        sources = [dict(item) for item in self.sources]
        by_name = {item['name_bytes_hex']:item for item in sources}
        positions = {item['name_bytes_hex']:number for number,item in enumerate(sources)}
        next_slot = max((int(Path(item['path']).stem) for item in sources), default=-1)+1
        changed, added, replaced = [], 0, 0
        for item in incoming_sources:
            checkpoint()
            previous = by_name.get(item['name_bytes_hex'])
            incoming = delta_raw/Path(item['path']).name
            if previous and all(previous[key] == item[key] for key in ('bytes', 'sha256')):
                with merge_io('discard_overlap',item['path']):
                    remove_file(incoming,checkpoint)
                continue  # The 15-day overlap must not duplicate unchanged mail.
            if previous:
                relative = previous['path']
                replaced += 1
            else:
                if next_slot > 999999:
                    raise CollectionError('메일 원본 파일 수가 보관 한도를 넘었습니다.')
                relative = 'mail/raw/%06d.bin' % next_slot
                next_slot += 1
                added += 1
            (destination/relative).parent.mkdir(parents=True, exist_ok=True)
            with merge_io('replace_mail',relative):
                # Replace the staged directory entry without first deleting
                # its hard link. Never truncate or chmod the old inode.
                replace_file(incoming,destination/relative,checkpoint)
            entry = dict(item, path=relative)
            if previous:
                sources[positions[item['name_bytes_hex']]] = entry
            else:
                positions[item['name_bytes_hex']] = len(sources)
                sources.append(entry)
            by_name[item['name_bytes_hex']] = entry
            files[relative] = {key:item[key] for key in ('bytes','sha256')}
            changed.append(relative)
        if delta_raw.exists():
            if any(delta_raw.iterdir()):
                raise CollectionError('추가 메일 준비 폴더에 반영하지 못한 원본이 남아 있습니다. 기존 백업을 유지하고 중단했습니다.')
            try:
                delta_raw.rmdir()
            except OSError:
                log('추가 백업의 빈 임시 폴더 정리를 확인하지 못했습니다. 백업 자료 검증은 계속합니다.')
        with merge_io('save_metadata','catalog.json'):
            save(destination/'catalog.json', {'items':list(catalog.values())})
        with merge_io('save_metadata','mail/sources.json'):
            save(destination/'mail/sources.json', {'user':current['user'], 'sources':sources, 'raw_files_complete':True})
        for name in ('catalog.json', 'mail/sources.json', 'catalog.sqlite3'):
            files[name] = digest_file(destination/name, checkpoint)
        current['files'] = files
        current['counts']['approval'] = len(catalog)
        current['counts']['mail_files'] = len(sources)
        current['warnings'] = list(dict.fromkeys(self.manifest.get('warnings',[])+current.get('warnings',[])))
        if not approvals:
            for key in ('snapshot_rows','document_presentation'):
                if key in self.manifest:
                    current[key] = self.manifest[key]
            if self.approval_checkpoint is not None:
                current['approval_checkpoint']=self.approval_checkpoint
        if not mail and ('mail_checkpoint' in self.manifest or self.sources or
                         self.manifest['live_export'].get('mail')):
            current['mail_checkpoint'] = self.mail_checkpoint
        for key in ('approval_status_map',):
            if key in self.manifest and key not in current:
                current[key] = self.manifest[key]
        current['local_processing'] = {'state':'pending', 'changed_mail_sources':changed}
        current['last_incremental'] = {'protocol':PROTOCOL, 'since':self.since, 'merged':True,
                                       'lookback_days':LOOKBACK_DAYS,
                                       'added_mail_files':added, 'changed_mail_files':replaced,
                                       'retained_mail_files':len(sources)-len(changed)}
        if approvals:
            current['last_incremental']['approval_since']=self.approval_since
            current['last_incremental']['received_approval_documents']=len(new_catalog['items'])
        with merge_io('save_metadata','manifest.json'):
            save(destination/'manifest.json', current)
        log('기존 메일 원본을 유지하고 새 파일 %d개·변경 파일 %d개만 반영합니다.' % (added,replaced))
        return changed

    @staticmethod
    def copy(source, target, expected, checkpoint, report, *, relative=''):
        require_space(target.parent, expected['bytes']+64*1024*1024, '기존 백업 반영')
        digest, size = hashlib.sha256(), 0
        # The copy is private and writable even when a source is read-only.
        # Source permissions/attributes are never changed.
        with merge_io('copy_previous',relative):
            with merge_io('read_previous',relative):
                incoming=retry_file_io(lambda:source.open('rb'),checkpoint)
            with incoming, retry_file_io(lambda:target.open('xb'),checkpoint) as outgoing:
                while block := incoming.read(1024*1024):
                    checkpoint()
                    outgoing.write(block)
                    digest.update(block)
                    size += len(block)
                    if expected['bytes'] > 16*1024*1024:
                        report('reuse',size,expected['bytes'])
        if {'bytes':size, 'sha256':digest.hexdigest()} != expected:
            raise CollectionError('기존 백업 파일의 SHA-256이 변경되었습니다. 파일 무결성을 검사하세요.')

    def commit(self, staging, checkpoint, log):
        with update_lock(self.root), merge_io('commit_backup','manifest.json'):
            return self._commit(staging,checkpoint,log)

    def _commit(self, staging, checkpoint, log):
        checkpoint()
        path=self.root/'manifest.json'
        if (path.stat().st_size!=self.original_manifest_signature['bytes'] or
                digest_file(path,checkpoint)!=self.original_manifest_signature):
            raise CollectionError('작업 중 기존 백업이 변경되어 반영을 중단했습니다. 다른 백업·갱신 작업이 끝난 뒤 다시 실행하세요.')
        previous = self.root.parent/('.segio-previous-'+uuid.uuid4().hex)
        journal = self.journal(self.root)
        if journal.exists():
            raise CollectionError('다른 백업 반영이 진행 중입니다. 기존 백업을 유지하고 반영을 중단했습니다.')
        # A journal survives a process/power loss between the two renames.
        with journal.open('xb') as output:
            output.write(json.dumps({'target':self.root.name, 'previous':previous.name}).encode())
            output.flush()
            os.fsync(output.fileno())
        try:
            checkpoint()
            replace_file(self.root,previous,checkpoint)
            try:
                replace_file(staging,self.root,checkpoint)
            except BaseException:
                replace_file(previous,self.root)
                raise
        except BaseException:
            if self.root.exists():
                journal.unlink(missing_ok=True)
            raise
        try:
            journal.unlink(missing_ok=True)
        except OSError:
            log('백업 반영은 완료했지만 PC 반영 확인 기록 정리를 확인하지 못했습니다: '+str(journal))
        try:
            shutil.rmtree(previous)
        except OSError:
            log('백업 반영은 완료했지만 PC의 이전 사본 폴더 정리를 확인하지 못했습니다: '+str(previous))
        log('추가 자료를 기존 백업 폴더에 반영했습니다: '+str(self.root))
        return self.root
