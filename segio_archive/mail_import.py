"""Index original server mail files locally; no webmail requests or source writes."""
from __future__ import annotations
import hashlib
import html
import json
import mailbox
import re
import shutil
import sqlite3
from email import policy
from email.header import Header, decode_header
from email.parser import BytesHeaderParser, BytesParser
from pathlib import Path

from bs4 import BeautifulSoup

from .archive import safe_name
from .web import CollectionError

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


def header_text(value):
    try:
        return ''.join(decode_bytes(part, charset) if isinstance(part, bytes) else part
                       for part, charset in decode_header(value if isinstance(value, Header) else str(value or '')))
    except (ValueError, LookupError):
        return str(value or '')


def build_local_catalog(root: Path, user: str, checkpoint=lambda: None):
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('user') != user or not isinstance(manifest.get('live_export'), dict):
        raise CollectionError('실시간 추출 정보 또는 로그인 ID가 일치하지 않습니다.')
    files = manifest['files']
    from .db_client import safe_member
    def local(name):
        safe_member(name)
        if name not in files:
            raise CollectionError('검색 색인의 파일이 검증 목록에 없습니다.')
        return root/name
    def write(name, body):
        path = root/name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = body.encode('utf-8') if isinstance(body, str) else body
        path.write_bytes(data)
        files[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    catalog = json.loads((root/'catalog.json').read_text(encoding='utf-8'))
    items = []
    for item in catalog['items']:
        local(item['path'])
        local(item['json'])
        for attachment in item['attachments']:
            local(attachment['path'])
        items.append({'id': item['id'], 'kind': 'approval', 'title': item['title'],
                      'folders': ['현재 결재' if item['source']=='active' else '과거 결재'],
                      'body': item['search'], 'html': item['path'], 'json': item['json'],
                      'pdf': '', 'eml': '', 'attachments': item['attachments'],
                      'captured_at': manifest['finished_at'], 'status': 'captured'})
    sources_file = root/'mail/sources.json'
    sources = json.loads(sources_file.read_text(encoding='utf-8')) if sources_file.exists() else {'sources': []}
    if sources.get('user', user) != user:
        raise CollectionError('메일 원본의 사용자 ID가 일치하지 않습니다.')
    for source in sources['sources']:
        name = source['path']
        if (not re.fullmatch(r'mail/raw/[0-9]{6}\.bin', name) or name not in files or
            source['bytes']!=files[name]['bytes'] or source['sha256']!=files[name]['sha256']):
            raise CollectionError('메일 원본 경로 또는 검증 정보가 일치하지 않습니다.')
    raw_bytes = sum(source['bytes'] for source in sources['sources'])
    if shutil.disk_usage(root).free < raw_bytes * 2 + 64*1024*1024:
        raise CollectionError('메일 본문과 첨부 색인을 만들 저장 공간이 부족합니다.')
    issues = []
    mail_count = 0

    def add_message(source, stream, size):
        nonlocal mail_count
        checkpoint()
        identity = f'mail-{mail_count:08d}'
        directory = f'mail/messages/{mail_count:08d}'
        header = bytearray()
        while len(header) < 1024*1024:
            line = stream.readline(65536)
            if not line or line in (b'\n', b'\r\n'):
                break
            header.extend(line)
        message = BytesHeaderParser(policy=policy.compat32).parsebytes(bytes(header))
        title = header_text(message.get('Subject')) or '제목 없음'
        sender = header_text(message.get('From'))
        receiver = header_text(message.get('To'))
        date = header_text(message.get('Date'))
        attachments = []
        text = ''
        if size <= PARSE_LIMIT:
            stream.seek(0)
            parsed = BytesParser(policy=policy.compat32).parsebytes(stream.read(PARSE_LIMIT+1))
            plain, rich = [], []
            for number, part in enumerate(parsed.walk()):
                checkpoint()
                if part.is_multipart():
                    continue
                payload = part.get_payload(decode=True) or b''
                name = part.get_filename()
                if name or part.get_content_disposition()=='attachment':
                    filename = header_text(name) or 'attachment.bin'
                    relative = f'{directory}/attachments/{number}-{safe_name(filename, 90)}'
                    write(relative, payload)
                    attachments.append({'name': filename, 'path': relative, 'bytes': len(payload)})
                elif part.get_content_type()=='text/plain':
                    plain.append(decode_bytes(payload, part.get_content_charset()))
                elif part.get_content_type()=='text/html':
                    soup = BeautifulSoup(decode_bytes(payload, part.get_content_charset()), 'html.parser')
                    for tag in soup.select('script,style,iframe,object,svg,form,img'):
                        tag.decompose()
                    rich.append(soup.get_text('\n', strip=True))
            text = '\n'.join(plain or rich)
        else:
            text = '대용량 메일: 제목과 헤더를 색인했습니다. 전체 원본과 첨부는 원본 메일 파일에 보존되어 있습니다.'
            issues.append({'kind': 'large_message_headers_only', 'id': identity})
        body = f'''<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{html.escape(title)}</title><style>body{{font:16px 'Malgun Gothic',sans-serif;margin:24px;overflow-wrap:anywhere}}pre{{white-space:pre-wrap}}</style>
<p><a href="../../../index.html">보관함 목록</a> · <a href="../../../{source['path']}">원본 메일 파일</a></p>
<h1>{html.escape(title)}</h1><p>보낸 사람: {html.escape(sender)}<br>받는 사람: {html.escape(receiver)}<br>일시: {html.escape(date)}<br>메일함: {html.escape(source['name'])}</p>
<pre>{html.escape(text)}</pre>'''
        if attachments:
            body += '<h2>첨부</h2>' + ''.join(f'<p><a target="_blank" rel="noopener noreferrer" href="../../../{html.escape(a["path"], quote=True)}">{html.escape(a["name"])}</a></p>' for a in attachments)
        path = f'{directory}/document.html'
        write(path, body)
        items.append({'id': identity, 'kind': 'mail', 'title': title, 'folders': [source['name']],
                      'body': '\n'.join((sender, receiver, date, text)), 'date': date, 'html': path, 'pdf': '',
                      'eml': source['path'] if source['format']=='rfc822' else '',
                      'mbox': source['path'] if source['format']=='mbox' else '',
                      'attachments': attachments, 'captured_at': manifest['finished_at'], 'status': 'raw_preserved'})
        mail_count += 1

    for source in sources['sources']:
        checkpoint()
        name = source['path']
        # The transfer manifest has already verified the files. Never let mail
        # metadata name a document, config, or path outside its raw namespace.
        if not re.fullmatch(r'mail/raw/[0-9]{6}\.bin', name) or name not in files:
            raise CollectionError('메일 원본 경로가 올바르지 않습니다.')
        path = root/name
        with path.open('rb') as stream:
            prefix = stream.read(8192)
        is_mbox = not prefix or prefix.startswith(b'From ')
        # Postfix/Maildir messages can start with Delivered-To or an X-* field.
        # Require a header-shaped first line and a mail header in the prefix.
        is_eml = (not is_mbox and bool(re.match(rb'[A-Za-z][A-Za-z0-9-]*:', prefix)) and
                  bool(re.search(rb'(?im)^(return-path|delivered-to|received|date|from|to|subject|message-id|mime-version|content-type):', prefix)))
        if not is_mbox and not is_eml:
            source['format'] = 'unrecognized'
            issues.append({'kind': 'unrecognized_mail_file', 'source': name})
            continue
        renamed = str(Path(name).with_suffix('.mbox' if is_mbox else '.eml')).replace('\\','/')
        path.rename(root/renamed)
        files[renamed] = files.pop(name)
        source['path'] = renamed
        source['format'] = 'mbox' if is_mbox else 'rfc822'
        if is_mbox:
            box = mailbox.mbox(root/renamed, create=False)
            try:
                for key in box.iterkeys():
                    checkpoint()
                    with box.get_file(key) as stream:
                        stream.seek(0, 2)
                        size = stream.tell()
                        stream.seek(0)
                        add_message(source, stream, size)
            finally:
                box.close()
        else:
            with (root/renamed).open('rb') as stream:
                add_message(source, stream, source['bytes'])
    if sources_file.exists():
        write('mail/sources.json', json.dumps(sources, ensure_ascii=False, indent=2))
    write('mail/index-issues.json', json.dumps(issues, ensure_ascii=False, indent=2))
    manifest['counts']['mail'] = mail_count
    manifest['coverage'].append({'kind':'mail_index', 'status':'indexed_with_warnings' if issues else 'indexed',
                                 'saved':mail_count, 'issues':len(issues)})
    if issues:
        manifest['warnings'].append(f'메일 원본은 보존했습니다. 제목·본문 색인 확인 사항 {len(issues)}개: mail/index-issues.json')
    # Rebuild a local SQLite database; do not open a database supplied remotely.
    database = root/'catalog.sqlite3'
    database.unlink(missing_ok=True)
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute('CREATE TABLE items (id TEXT PRIMARY KEY, kind TEXT, title TEXT, folders TEXT, body TEXT, html TEXT)')
            connection.execute('CREATE TABLE document_records (id TEXT PRIMARY KEY, payload TEXT)')
            connection.execute('CREATE TABLE mail_dates (id TEXT PRIMARY KEY, header_date TEXT)')
            for item in items:
                checkpoint()
                connection.execute('INSERT INTO items VALUES (?,?,?,?,?,?)', (item['id'],item['kind'],item['title'],json.dumps(item['folders'],ensure_ascii=False),item['body'],item['html']))
                if item['kind']=='mail':
                    connection.execute('INSERT INTO mail_dates VALUES (?,?)',(item['id'],item.get('date','')))
                if item['kind']=='approval':
                    connection.execute('INSERT INTO document_records VALUES (?,?)', (item['id'],local(item['json']).read_text(encoding='utf-8')))
    finally:
        connection.close()
    digest, size = hashlib.sha256(), 0
    with database.open('rb') as stream:
        while block := stream.read(1024*1024):
            checkpoint()
            size += len(block)
            digest.update(block)
    files['catalog.sqlite3'] = {'bytes':size,'sha256':digest.hexdigest()}
    write('catalog.json', json.dumps({'items':items}, ensure_ascii=False))
    manifest['viewer']='local-db/1'
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2), encoding='utf-8')
    from .search_page import install_search_page
    return install_search_page(root,manifest,checkpoint)
