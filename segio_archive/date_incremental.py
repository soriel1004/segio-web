"""Date-selected mail deltas, staged beside and committed to an existing backup."""
import errno
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .archive import atomic_write
from .compact_mail import digest_file
from .incremental import Baseline
from .storage import require_space
from .web import CollectionError

PROTOCOL = 'segio-mail-date/1'
REVISION = 'file-time-merge-1'


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
        except OSError:
            raise CollectionError('다른 작업이 기존 백업을 반영 중입니다. 해당 작업이 끝난 뒤 다시 실행하세요.') from None
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
        with update_lock(root):
            self.recover(root)
        if (root/'mail/sources.json').exists():
            super().__init__(root, user, checkpoint)
        else:
            self.root, self.entries = root, {}
        try:
            path = root/'manifest.json'
            if path.is_symlink() or path.stat().st_size > 16*1024*1024:
                raise ValueError()
            self.original_manifest = path.read_bytes()
            self.manifest = json.loads(self.original_manifest)
            if (self.manifest.get('user') != user or self.manifest.get('format') != 'segio-db-html/1' or
                    not isinstance(self.manifest.get('live_export'), dict) or
                    self.manifest.get('state') not in ('collected', 'collected_with_warnings') or
                    self.manifest.get('local_processing', {}).get('state') == 'pending'):
                raise ValueError()
            value = self.manifest.get('mail_checkpoint', self.manifest['live_export'].get('requested_at'))
            if value is None:
                value = datetime.fromisoformat(self.manifest['finished_at'].replace('Z', '+00:00')).timestamp()
            self.since = int(value)
            if isinstance(value, bool) or self.since < 1:
                raise ValueError()
            for name in ('catalog.sqlite3', 'catalog.json'):
                if digest_file(self.raw_path(name), checkpoint) != self.manifest['files'][name]:
                    raise ValueError()
            self.sources = (json.loads((root/'mail/sources.json').read_bytes()).get('sources', [])
                            if (root/'mail/sources.json').exists() else [])
            if self.manifest.get('viewer') != 'sqlite-mail/1':
                raise ValueError()
            from .portable_mail import VERSION
            if self.manifest.get('portable_mail',{}).get('version') != VERSION:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise CollectionError('이전 백업의 완료 상태·기준 시각·검색 DB를 확인하지 못했습니다. 먼저 ‘기존 백업 갱신’으로 정상 완료한 본인 폴더를 선택하세요.') from None

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
            raise CollectionError('이전 백업 반영이 중단된 기록이 있습니다. 기존 폴더와 .segio-previous- 폴더를 보존하고 PC 폴더 접근 상태를 확인하세요.') from None

    def validate_delta(self, destination):
        manifest = json.loads((destination/'manifest.json').read_bytes())
        delta = manifest.get('mail_delta', {})
        if (delta.get('protocol') != PROTOCOL or delta.get('revision') != REVISION or
                delta.get('since') != self.since or type(manifest.get('mail_checkpoint')) is not int or
                manifest['mail_checkpoint'] < self.since):
            raise CollectionError('서버가 요청한 백업 기준 시각과 일치하는 추가 메일을 반환하지 않았습니다. 서버 공용 tools를 갱신하세요.')
        source_path = destination/'mail/sources.json'
        if source_path.stat().st_size > 16*1024*1024:
            raise CollectionError('추가 메일 원본 목록이 너무 큽니다.')
        sources = json.loads(source_path.read_bytes())
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

    def prepare(self, destination, checkpoint, report, log, *, approvals=True, mail=True):
        """Keep old raw/pages immutable; only private metadata is rewritten."""
        from .db_client import safe_member
        destination = Path(destination)
        current = json.loads((destination/'manifest.json').read_bytes())
        incoming_sources = (json.loads((destination/'mail/sources.json').read_bytes())['sources'] if mail else [])
        delta_raw = destination/'.incoming-mail'
        if (destination/'mail/raw').exists():
            os.replace(destination/'mail/raw', delta_raw)
        old_catalog = json.loads((self.root/'catalog.json').read_bytes())
        new_catalog = json.loads((destination/'catalog.json').read_bytes())
        catalog = {item['id']:item for item in old_catalog['items']}
        if approvals:
            catalog.update((item['id'],item) for item in new_catalog['items'])
        old_files, new_files = self.manifest['files'], dict(current['files'])
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
            source = self.raw_path(name)
            if source.stat().st_size != expected['bytes']:
                raise CollectionError('기존 백업 파일의 크기가 변경되었습니다. 파일 무결성을 검사하세요.')
            target = destination/name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise CollectionError('기존 백업 반영 경로가 중복됩니다.')
            if name.startswith(('mail/raw/', 'mail/offline/')):
                try:
                    os.link(source, target)
                except OSError as exc:
                    if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EACCES):
                        raise
                    self.copy(source, target, expected, checkpoint, report)
            else:
                self.copy(source, target, expected, checkpoint, report)
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
                incoming.unlink()
                continue  # Inclusive second-boundary overlap is not a duplicate.
            if previous:
                relative = previous['path']
                (destination/relative).unlink()  # Detach the old immutable inode.
                replaced += 1
            else:
                if next_slot > 999999:
                    raise CollectionError('메일 원본 파일 수가 보관 한도를 넘었습니다.')
                relative = 'mail/raw/%06d.bin' % next_slot
                next_slot += 1
                added += 1
            (destination/relative).parent.mkdir(parents=True, exist_ok=True)
            os.replace(incoming, destination/relative)
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
            delta_raw.rmdir()
        save(destination/'catalog.json', {'items':list(catalog.values())})
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
        if not mail and 'mail_checkpoint' in self.manifest:
            current['mail_checkpoint'] = self.manifest['mail_checkpoint']
        for key in ('approval_status_map',):
            if key in self.manifest and key not in current:
                current[key] = self.manifest[key]
        current['local_processing'] = {'state':'pending', 'changed_mail_sources':changed}
        current['last_incremental'] = {'protocol':PROTOCOL, 'since':self.since, 'merged':True,
                                       'added_mail_files':added, 'changed_mail_files':replaced,
                                       'retained_mail_files':len(sources)-len(changed)}
        save(destination/'manifest.json', current)
        log('기존 메일 원본을 유지하고 새 파일 %d개·변경 파일 %d개만 반영합니다.' % (added,replaced))
        return changed

    @staticmethod
    def copy(source, target, expected, checkpoint, report):
        require_space(target.parent, expected['bytes']+64*1024*1024, '기존 백업 반영')
        digest, size = hashlib.sha256(), 0
        with source.open('rb') as incoming, target.open('xb') as outgoing:
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
        with update_lock(self.root):
            return self._commit(staging,checkpoint,log)

    def _commit(self, staging, checkpoint, log):
        checkpoint()
        if (self.root/'manifest.json').read_bytes() != self.original_manifest:
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
            os.replace(self.root, previous)
            try:
                os.replace(staging, self.root)
            except BaseException:
                os.replace(previous, self.root)
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
