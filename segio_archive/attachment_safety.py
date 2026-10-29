"""Respect security blocks on derived mail attachments without recreating them."""
import json
import re

from .archive import atomic_write
from .web import CollectionError

ISSUES = {
    'attachment_security':'보안 프로그램이 첨부 파일 처리를 차단했습니다. 해당 첨부는 미확보 상태이며 보안 프로그램의 보호 기록을 확인하세요.',
    'attachment_unavailable':'추출했던 첨부 파일이 없어졌거나 변경되어 확보하지 못했습니다. 삭제·격리 여부를 확인하세요. 해당 파일을 자동 복원하지 않았습니다.',
    'attachment_unverified':'이전 변환이 중단되어 이 첨부의 확보 상태를 확인하지 못했습니다. 삭제·격리 가능성이 있어 자동으로 재생성하지 않았습니다.',
}


class AttachmentUnavailable(Exception):
    def __init__(self, issue):
        self.issue = issue
        super().__init__(ISSUES[issue])


def attachment_name(name):
    return isinstance(name,str) and bool(re.fullmatch(r'mail/offline/mail-[0-9]{9}/attachments/[^/\\\x00]+',name)) and name.rsplit('/',1)[1] not in ('.','..')


def legacy_attachment_alias(name):
    """Recognize only the old 80-character mail attachment truncation bug."""
    if not isinstance(name,str):
        return None
    match=re.fullmatch(r'(mail/offline/mail-[0-9]{9}/attachments/[0-9]+-)([^/\\\x00]+)',name)
    if not match:
        return None
    leaf=match[2]
    if (not leaf.endswith((' ','.')) or
            not (len(leaf)==80 or (len(leaf)==85 and leaf.startswith('file_')))):
        return None
    normalized=leaf.rstrip(' .')
    return match[1]+normalized if normalized else None


def security_error(exc):
    # Windows ERROR_VIRUS_INFECTED / ERROR_VIRUS_DELETED. Ordinary access
    # denied, locks, full disks and raw-source failures remain fatal errors.
    return isinstance(exc,OSError) and getattr(exc,'winerror',None) in (225,226)


def attachment_io(operation, name, *, missing=False):
    try:
        return operation()
    except OSError as exc:
        if attachment_name(name):
            if security_error(exc):
                raise AttachmentUnavailable('attachment_security') from None
            if missing and isinstance(exc,FileNotFoundError):
                raise AttachmentUnavailable('attachment_unavailable') from None
        raise


class AttachmentStatus:
    def __init__(self, root, directory, fingerprint):
        self.root, self.directory, self.fingerprint = root,directory,fingerprint
        self.path = root/directory/'attachment-status.json'
        self.skipped = {}
        if not self.path.exists():
            return
        try:
            if self.path.is_symlink() or self.path.stat().st_size>4*1024*1024:
                raise ValueError()
            data=json.loads(self.path.read_bytes())
            if data.get('source') != fingerprint:
                return  # A replaced mailbox has a different source fingerprint.
            skipped=data['skipped']
            if (not isinstance(skipped,dict) or any(not self.valid_name(name) or issue not in ISSUES
                                                  for name,issue in skipped.items())):
                raise ValueError()
            self.skipped=skipped
        except (ValueError,KeyError,TypeError,AttributeError):
            raise CollectionError('첨부 미확보 기록을 확인하지 못했습니다. 기존 폴더와 보안 프로그램의 보호 기록을 보존하세요.') from None

    def valid_name(self,name):
        return attachment_name(name) and name.startswith(self.directory+'/attachments/')

    def record(self,name,issue):
        if not self.valid_name(name) or issue not in ISSUES:
            raise CollectionError('첨부 미확보 기록의 경로 또는 코드가 올바르지 않습니다.')
        if self.skipped.get(name) == 'attachment_security':
            return
        self.skipped[name]=issue
        # Store only path + fixed reason, never the blocked payload. Persist
        # before continuing, so recovery does not re-extract this attachment.
        atomic_write(self.path,json.dumps({'source':self.fingerprint,'skipped':self.skipped},ensure_ascii=False).encode())
