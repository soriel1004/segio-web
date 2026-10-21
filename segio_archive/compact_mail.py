"""Disk-backed mail title index and portable mail pages from original MBOX."""
from __future__ import annotations
from contextlib import closing
from email import policy
from email.parser import BytesHeaderParser, BytesParser
import hashlib
import html
import json
import re
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup
from .mail_import import header_text, decode_bytes, PARSE_LIMIT
from .web import CollectionError


def digest_file(path, checkpoint=lambda: None):
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            checkpoint()
            digest.update(block)
            size += len(block)
    return {'bytes': size, 'sha256': digest.hexdigest()}


def mbox_spans(path, checkpoint=lambda: None):
    """Scan bounded chunks, including delimiters straddling chunk boundaries."""
    with path.open('rb') as scanner, path.open('rb') as envelopes:
        first = envelopes.readline(65537)
        if not first:
            return
        if not first.startswith(b'From ') or not first.endswith(b'\n'):
            raise CollectionError('MBOX 구분 줄을 확인하지 못했습니다. 원본은 보존됩니다.')
        start, position, carry = len(first), 0, b''
        while block := scanner.read(1024 * 1024):
            checkpoint()
            data = carry + block
            base, offset = position - len(carry), 0
            while (found := data.find(b'\nFrom ', offset)) >= 0:
                boundary = base + found + 1
                envelopes.seek(boundary)
                envelope = envelopes.readline(65537)
                if len(envelope) > 65536 or not envelope.endswith(b'\n'):
                    raise CollectionError('MBOX 구분 줄이 너무 깁니다. 원본은 보존됩니다.')
                yield start, boundary
                start = boundary + len(envelope)
                offset = found + 6
            position += len(block)
            carry = data[-5:]
        if position > start:
            yield start, position


def read_headers(stream, start, end):
    stream.seek(start)
    header = bytearray()
    while len(header) < 1024 * 1024 and stream.tell() < end:
        line = stream.readline(min(65536, end - stream.tell()))
        if not line or line in (b'\n', b'\r\n'):
            break
        header.extend(line)
    return BytesHeaderParser(policy=policy.compat32).parsebytes(bytes(header))


def build_compact_catalog(root: Path, user: str, checkpoint=lambda: None, log=lambda _: None):
    from .db_client import safe_member
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('user') != user or manifest.get('ssh_export', {}).get('authentication') != 'administrator_ssh':
        raise CollectionError('SSH 보관본의 직원 ID 또는 추출 형식이 일치하지 않습니다.')
    files = manifest['files']
    sources = json.loads((root/'mail/sources.json').read_text(encoding='utf-8')) if (root/'mail/sources.json').exists() else {'sources': []}
    if sources.get('user', user) != user:
        raise CollectionError('메일 원본의 직원 ID가 일치하지 않습니다.')
    database, temporary = root/'catalog.sqlite3', root/'catalog.sqlite3.partial'
    temporary.unlink(missing_ok=True)
    mail_count, issue_count = 0, 0
    with closing(sqlite3.connect(temporary)) as db:
        db.execute('CREATE TABLE items (id TEXT PRIMARY KEY, kind TEXT, title TEXT, folders TEXT, body TEXT, html TEXT)')
        db.execute('CREATE TABLE document_records (id TEXT PRIMARY KEY, payload TEXT)')
        db.execute('CREATE TABLE mail_records (id TEXT PRIMARY KEY, source TEXT, begin INTEGER, end INTEGER, sender TEXT, receiver TEXT, date TEXT)')
        catalog = json.loads((root/'catalog.json').read_text(encoding='utf-8'))
        for item in catalog['items']:
            checkpoint()
            for name in (item['path'], item['json']):
                safe_member(name)
                if name not in files:
                    raise CollectionError('결재 색인에 검증되지 않은 파일이 있습니다.')
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                       ('approval-'+item['id'], 'approval', item['title'], '결재 문서', item['search'], item['path']))
            db.execute('INSERT INTO document_records VALUES (?,?)',
                       ('approval-'+item['id'], (root/item['json']).read_text(encoding='utf-8')))
        for source in sources['sources']:
            checkpoint()
            name = source['path']
            safe_member(name)
            if (not name.startswith('mail/raw/') or name not in files or
                    any(source.get(key) != files[name][key] for key in ('bytes', 'sha256'))):
                raise CollectionError('메일 원본의 경로 또는 검증 정보가 일치하지 않습니다.')
            path = root/name
            kind = source.get('format')
            if kind == 'mbox':
                spans = mbox_spans(path, checkpoint)
            elif kind == 'rfc822':
                spans = [(0, source['bytes'])]
            else:
                issue_count += 1
                continue
            with path.open('rb') as stream:
                for start, end in spans:
                    checkpoint()
                    message = read_headers(stream, start, end)
                    title = header_text(message.get('Subject')) or '제목 없음'
                    sender, receiver, date = (header_text(message.get(key)) for key in ('From', 'To', 'Date'))
                    identity = f'mail-{mail_count:09d}'
                    db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                               (identity, 'mail', title, source['name'], '\n'.join((sender, receiver, date)), 'mail/'+identity))
                    db.execute('INSERT INTO mail_records VALUES (?,?,?,?,?,?,?)',
                               (identity, name, start, end, sender, receiver, date))
                    mail_count += 1
                    if mail_count % 1000 == 0:
                        db.commit()
                        log(f'메일 제목 색인 {mail_count:,}건')
        db.execute('CREATE INDEX title_index ON items(kind,title)')
        db.commit()
    temporary.replace(database)
    manifest['counts']['mail'] = mail_count
    manifest['viewer'] = 'sqlite-mail/1'
    manifest['coverage'] = [entry for entry in manifest['coverage'] if entry.get('kind') != 'mail_index']
    manifest['coverage'].append({'kind':'mail_index', 'status':'headers_indexed', 'saved':mail_count,
                                 'unrecognized_files':issue_count, 'body':'read_from_raw_on_demand'})
    warning = '메일은 제목·보낸 사람·받는 사람을 검색합니다. 본문·첨부는 원본에서 열람할 때 읽습니다.'
    if warning not in manifest['warnings']:
        manifest['warnings'].append(warning)
    if issue_count:
        manifest['warnings'].append(f'인식하지 못한 메일 원본 파일 {issue_count}개는 원본 그대로 보존했습니다.')
    files['catalog.sqlite3'] = digest_file(database, checkpoint)
    from .portable_mail import build_portable_mail
    manifest=build_portable_mail(root,manifest,checkpoint,log)
    from .search_page import install_search_page
    return install_search_page(root, manifest, checkpoint)


def search(root: Path, query='', kind='', page=0, mailbox='', category=''):
    from .mail_folders import MailFolders
    from .index_metadata import IndexMetadata, CATEGORIES, item_date
    query = query[:200].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    pattern = '%'+query+'%'
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        MailFolders(root).register(db)
        meta = IndexMetadata(root,db)
        folder_label="CASE WHEN i.kind='mail' THEN mailbox_label(i.folders) ELSE approval_label("+meta.payload_sql+") END"
        where = r"(i.title LIKE ? ESCAPE '\' OR i.body LIKE ? ESCAPE '\' OR i.folders LIKE ? ESCAPE '\' OR "+folder_label+r" LIKE ? ESCAPE '\')"
        parameters = [pattern]*4
        if kind in ('mail', 'approval'):
            where += ' AND i.kind=?'
            parameters.append(kind)
        if mailbox and kind!='approval':
            where+=" AND i.kind='mail' AND mailbox_key(i.folders)=?"
            parameters.append(mailbox)
        if category and kind!='mail':
            where += " AND i.kind='approval' AND approval_has("+meta.payload_sql+",?)=1"
            parameters.append(category)
        origin = ' FROM items i '+meta.joins+' WHERE '+where
        total = db.execute('SELECT count(*)'+origin, parameters).fetchone()[0]
        fields = 'i.id,i.kind,i.title,'+folder_label+",i.html,CASE WHEN i.kind='mail' THEN mailbox_key(i.folders) ELSE '' END,i.folders,"
        rows = db.execute('SELECT '+fields+meta.date_sql+','+meta.payload_sql+origin+
                          ' ORDER BY '+meta.order+' LIMIT 100 OFFSET ?', parameters+[max(0,min(page,1000000))*100]).fetchall()
    items=[]
    from .portable_mail import page_path
    for row in rows:
        item=dict(zip(('id','kind','title','folder','html','folder_id','source_folder'),row[:7]))
        item['date']=item_date(item['kind'], row[7], row[8])
        item['date_raw']=row[7] if item['kind']=='mail' else ''
        item['categories']=meta.categories(row[8]) if item['kind']=='approval' else []
        if item['kind']=='approval':
            item['folder']=' · '.join(CATEGORIES[key] for key in item['categories'])
        if item['kind']=='mail' and re.fullmatch(r'mail-[0-9]{9}',item['id']) and (root/page_path(item['id'])).is_file():
            item['html']=page_path(item['id'])
        items.append(item)
    return {'total':total,'items':items}


def mail_record(root: Path, identity: str):
    from .db_client import safe_member
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT m.*,i.title FROM mail_records m JOIN items i ON i.id=m.id WHERE m.id=?', (identity,)).fetchone()
    if row is None:
        raise ValueError('Unknown mail')
    record = dict(row)
    safe_member(record['source'])
    path = (root/record['source']).resolve()
    if (not path.is_relative_to(root.resolve()) or not record['source'].startswith('mail/raw/') or
            not 0 <= record['begin'] <= record['end'] <= path.stat().st_size):
        raise ValueError('Unsafe mail source')
    return record, path


def parse_message(root, identity):
    record, path = mail_record(root, identity)
    if record['end']-record['begin'] > PARSE_LIMIT:
        return record, None
    with path.open('rb') as stream:
        stream.seek(record['begin'])
        message = BytesParser(policy=policy.compat32).parsebytes(stream.read(record['end']-record['begin']))
    return record, message


def message_html(root, identity):
    record, message = parse_message(root, identity)
    plain, rich, attachments = [], [], []
    if message is not None:
        for number, part in enumerate(message.walk()):
            if part.is_multipart():
                continue
            if part.get_filename() or part.get_content_disposition() == 'attachment':
                attachments.append(f'<p><a target="_blank" rel="noopener noreferrer" href="{identity}/attachment/{number}">{html.escape(header_text(part.get_filename()) or "첨부")}</a></p>')
                continue
            payload = part.get_payload(decode=True) or b''
            if part.get_content_type() == 'text/plain':
                plain.append(decode_bytes(payload, part.get_content_charset()))
            elif part.get_content_type() == 'text/html':
                soup = BeautifulSoup(decode_bytes(payload, part.get_content_charset()), 'html.parser')
                for tag in soup.select('script,style,iframe,object,svg,form,img'):
                    tag.decompose()
                rich.append(soup.get_text('\n',strip=True))
    text = '\n'.join(plain or rich) if message is not None else '32 MiB보다 큰 메일입니다. 원본 EML을 저장해 메일 프로그램에서 열 수 있습니다.'
    return (f'<!doctype html><meta charset="utf-8"><title>{html.escape(record["title"])}</title>'
        '<style>body{font:12px sans-serif;margin:24px;overflow-wrap:anywhere}pre{white-space:pre-wrap;font:inherit}</style>'
        f'<a href="../index.html">목록</a> · <a href="{identity}/original.eml">원본 EML 저장</a>'
        f'<h1>{html.escape(record["title"])}</h1><p>{html.escape(record["sender"])} → {html.escape(record["receiver"])}<br>{html.escape(record["date"])}</p>'
        f'<pre>{html.escape(text)}</pre>'+''.join(attachments)).encode('utf-8')
