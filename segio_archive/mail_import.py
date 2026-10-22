"""Index original server mail files locally; no webmail requests or source writes."""
from __future__ import annotations
import json
import re
from email.header import Header, decode_header
from email.errors import MessageError
from pathlib import Path

from .web import CollectionError
from .file_ops import replace_file

PARSE_LIMIT = 32 * 1024 * 1024


def decode_bytes(data, charset=None):
    for codec in (charset, 'utf-8', 'cp949', 'latin-1'):
        if not codec:
            continue
        try:
            return data.decode(codec)
        except (LookupError, UnicodeError):
            pass
    return data.decode('utf-8', errors='replace')


def header_text(value, issues=None):
    raw=str(value or '')
    try:
        result=''.join(decode_bytes(part, charset) if isinstance(part, bytes) else part
                       for part, charset in decode_header(value if isinstance(value, Header) else raw))
    except (MessageError, ValueError, LookupError):
        if issues is not None:issues.add('header_encoding')
        result=raw
    # Preserve an invalid code point visibly in derived text. Raw RFC822/MBOX
    # bytes remain untouched; SQLite and HTML cannot store lone surrogates.
    safe=result.encode('utf-8','backslashreplace').decode('utf-8')
    if safe!=result and issues is not None:issues.add('header_unicode')
    return safe


def reconcile_mail_source(root, source, files, checkpoint):
    """Recover an interrupted extension rename using manifest SHA-256 evidence."""
    from .compact_mail import digest_file
    name = source['path']
    stem = str(Path(name).with_suffix('')).replace('\\','/')
    candidates = [stem + suffix for suffix in ('.bin','.mbox','.eml')]
    expected = {key:source.get(key) for key in ('bytes','sha256')}
    recorded = [value for value in candidates if files.get(value) == expected]
    present = [value for value in candidates if (root/value).is_file()]
    if (len(recorded)!=1 or len(present)!=1 or
            any((root/value).is_symlink() for value in candidates)):
        raise CollectionError('메일 원본 경로 또는 검증 정보가 일치하지 않습니다.')
    actual = present[0]
    if actual != name or recorded[0] != name:
        if digest_file(root/actual,checkpoint) != expected:
            raise CollectionError('중단된 메일 이름 변경을 복구할 수 없습니다. 원본 SHA-256이 일치하지 않습니다.')
        files[actual] = files.pop(recorded[0])
        source['path'] = actual
    return source['path']


def build_local_catalog(root: Path, user: str, checkpoint=lambda: None, log=lambda _:None, progress=None):
    """Personal-login archives use the same bounded SQLite/MIME pipeline as SSH."""
    from .archive import atomic_write
    from .compact_mail import build_compact_catalog, digest_file
    from .db_client import safe_member
    from .storage import require_space

    root = root.resolve()
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('user') != user or not isinstance(manifest.get('live_export'), dict):
        raise CollectionError('실시간 추출 정보 또는 그룹웨어 ID가 일치하지 않습니다.')
    files = manifest['files']
    sources_file = root/'mail/sources.json'
    sources = json.loads(sources_file.read_text(encoding='utf-8')) if sources_file.exists() else {'sources': []}
    if sources.get('user', user) != user:
        raise CollectionError('메일 원본의 사용자 ID가 일치하지 않습니다.')
    raw_bytes = 0
    report=progress or (lambda *args,**kwargs:None)
    report('prepare_mail',0,len(sources['sources']),unit='files')
    for number,source in enumerate(sources['sources'],1):
        checkpoint()
        name = source['path']
        safe_member(name)
        if not re.fullmatch(r'mail/raw/[0-9]{6}\.(bin|mbox|eml)', name):
            raise CollectionError('메일 원본 경로 또는 검증 정보가 일치하지 않습니다.')
        name = reconcile_mail_source(root,source,files,checkpoint)
        path = root/name
        if path.stat().st_size != source['bytes']:
            raise CollectionError('메일 원본 크기가 변경되었습니다.')
        raw_bytes += source['bytes']
        with path.open('rb') as stream:
            prefix = stream.read(8192)
        is_mbox = not prefix or prefix.startswith(b'From ')
        is_eml = (not is_mbox and bool(re.match(rb'[A-Za-z][A-Za-z0-9-]*:', prefix)) and
                  bool(re.search(rb'(?im)^(return-path|delivered-to|received|date|from|to|subject|message-id|mime-version|content-type):', prefix)))
        source['format'] = 'mbox' if is_mbox else 'rfc822' if is_eml else 'unrecognized'
        if not is_mbox and not is_eml:
            report('prepare_mail',number,len(sources['sources']),unit='files')
            continue
        renamed = str(Path(name).with_suffix('.mbox' if is_mbox else '.eml')).replace('\\','/')
        if renamed != name:
            if (root/renamed).exists():
                raise CollectionError('메일 원본 이름이 중복됩니다.')
            replace_file(path, root/renamed, checkpoint)
            files[renamed] = files.pop(name)
            source['path'] = renamed
        # Persist each rename before the next cancellable operation.
        atomic_write(sources_file, json.dumps(sources,ensure_ascii=False,indent=2).encode('utf-8'))
        files['mail/sources.json'] = digest_file(sources_file)
        atomic_write(root/'manifest.json', json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
        report('prepare_mail',number,len(sources['sources']),unit='files')
    if sources_file.exists():
        atomic_write(sources_file, json.dumps(sources,ensure_ascii=False,indent=2).encode('utf-8'))
        files['mail/sources.json'] = digest_file(sources_file)
    atomic_write(root/'manifest.json', json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
    # Raw messages stay intact; reserve another raw-mail-sized allowance for
    # decoded attachments, bounded body previews and filesystem metadata.
    require_space(root, raw_bytes + max(512*1024*1024, raw_bytes//20), '메일 본문·첨부 생성')
    result = build_compact_catalog(root, user, checkpoint, log, progress)
    result.pop('local_processing', None)
    atomic_write(root/'manifest.json', json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))
    return result
