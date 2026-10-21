"""Render local MBOX records to static HTML and attachments with bounded reads."""
from contextlib import closing
from email import policy
from email.parser import BytesHeaderParser
import base64
import hashlib
import html
import json
import quopri
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup

from .archive import atomic_write, safe_name
from .compact_mail import digest_file
from .mail_import import decode_bytes, header_text
from .web import CollectionError

VERSION = 'static-mail/1'
BLOCK = 65536
TEXT_LIMIT = 8 * 1024 * 1024


def page_path(identity):
    if not re.fullmatch(r'mail-[0-9]{9}', identity):
        raise CollectionError('메일 식별자 형식이 올바르지 않습니다.')
    return f'mail/offline/{identity}/index.html'


def local_path(root, name):
    path = root/name
    if not path.resolve().is_relative_to(root):
        raise CollectionError('메일 열람 파일 경로가 보관 폴더를 벗어납니다.')
    return path


def chunks(stream, start, end, checkpoint):
    stream.seek(start)
    while stream.tell() < end:
        checkpoint()
        block = stream.read(min(BLOCK, end-stream.tell()))
        if not block:
            raise CollectionError('메일 원본이 잘렸습니다.')
        yield block


def headers(stream, start, end):
    stream.seek(start)
    data = bytearray()
    while stream.tell() < end:
        line = stream.readline(min(BLOCK, end-stream.tell()))
        if not line or line in (b'\n', b'\r\n'):
            break
        data.extend(line)
        if len(data) > 1024*1024:
            raise CollectionError('메일 헤더가 너무 큽니다.')
    return BytesHeaderParser(policy=policy.compat32).parsebytes(bytes(data)), stream.tell()


def mime_parts(stream, start, end, checkpoint, depth=0):
    """Find MIME leaves by offsets, never loading a full attachment/message."""
    if depth > 20:
        raise CollectionError('메일 MIME 중첩 단계가 너무 많습니다.')
    message, body = headers(stream,start,end)
    if message.get_content_maintype() != 'multipart':
        return message, [(message,body,end)]
    boundary = message.get_boundary()
    if not boundary or len(boundary)>200:
        raise CollectionError('메일 MIME 구분자가 올바르지 않습니다.')
    try:
        marker = b'--'+boundary.encode('ascii')
    except UnicodeError:
        raise CollectionError('메일 MIME 구분자 인코딩이 올바르지 않습니다.') from None
    delimiters, line_start = [], True
    stream.seek(body)
    while stream.tell()<end:
        checkpoint()
        position=stream.tell()
        line=stream.readline(min(BLOCK,end-position))
        if not line:
            raise CollectionError('메일 원본이 잘렸습니다.')
        value=line.rstrip(b'\r\n \t') if line_start else b''
        if value in (marker,marker+b'--'):
            if len(delimiters)>10000:
                raise CollectionError('메일의 MIME 부분이 너무 많습니다.')
            delimiters.append((position,stream.tell(),value==marker+b'--'))
            if value==marker+b'--':
                break
        line_start=line.endswith(b'\n')
    leaves=[]
    for number,(_,after,closed) in enumerate(delimiters):
        if closed:
            break
        stop=delimiters[number+1][0] if number+1<len(delimiters) else end
        # The newline immediately preceding a delimiter belongs to that delimiter.
        if stop>after:
            stream.seek(max(after,stop-2))
            tail=stream.read(stop-max(after,stop-2))
            stop-=2 if tail.endswith(b'\r\n') else int(tail.endswith(b'\n'))
        _,children=mime_parts(stream,after,stop,checkpoint,depth+1)
        leaves.extend(children)
        if len(leaves)>10000:
            raise CollectionError('메일의 MIME 부분이 너무 많습니다.')
    if not delimiters:
        raise CollectionError('메일 MIME 본문의 구분자를 찾지 못했습니다.')
    return message,leaves


def decoded(stream, start, end, encoding, checkpoint):
    encoding=encoding.lower().strip()
    carry=b''
    for block in chunks(stream,start,end,checkpoint):
        if encoding=='base64':
            block=carry+re.sub(rb'[^A-Za-z0-9+/=]',b'',block)
            size=len(block)//4*4
            carry=block[size:]
            if size:
                yield base64.b64decode(block[:size])
        elif encoding=='quoted-printable':
            block=carry+block
            split=block.rfind(b'=',max(0,len(block)-2))
            carry=block[split:] if split>=0 else b''
            yield quopri.decodestring(block[:split] if split>=0 else block)
        elif encoding in ('','7bit','8bit','binary'):
            yield block
        else:
            raise CollectionError('지원하지 않는 메일 전송 인코딩: '+encoding[:40])
    if carry:
        yield base64.b64decode(carry+b'='*((-len(carry))%4)) if encoding=='base64' else quopri.decodestring(carry)


def safe_html(text, images):
    soup=BeautifulSoup(text,'html.parser')
    for tag in soup.select('script,style,iframe,object,embed,svg,math,form,template,head'):
        tag.decompose()
    allowed=set('p br div span table thead tbody tfoot tr td th ul ol li strong em b i u s hr blockquote pre code h1 h2 h3 h4 h5 h6 img a'.split())
    for tag in list(soup.find_all(True)):
        if tag.name not in allowed:
            tag.unwrap()
            continue
        old=dict(tag.attrs)
        tag.attrs={}
        if tag.name=='img':
            source=old.get('src','')
            image=images.get(source[4:].strip('<>')) if source.lower().startswith('cid:') else None
            if not image:
                tag.decompose()
                continue
            tag['src']=image
            tag['alt']=old.get('alt','본문 이미지')
        if tag.name in ('td','th'):
            for name in ('colspan','rowspan'):
                if str(old.get(name,'')).isdigit():
                    tag[name]=str(min(1000,int(old[name])))
    return str(soup)


def render_record(root, manifest, stream, record, checkpoint):
    directory=Path(page_path(record['id'])).parent.as_posix()
    local_path(root,directory).mkdir(parents=True,exist_ok=True)
    source_info=manifest['files'][record['source']]
    fingerprint=[VERSION,record['source'],record['begin'],record['end'],source_info['sha256']]
    state_path=local_path(root,directory+'/state.json')
    try:
        state=json.loads(state_path.read_text(encoding='utf-8'))
        if (state['source']==fingerprint and directory+'/index.html' in state['files']
                and all(name.startswith(directory+'/') for name in state['files'])
                and all(digest_file(local_path(root,name),checkpoint)==value for name,value in state['files'].items())):
            manifest['files'].update(state['files'])
            manifest['files'][directory+'/state.json']=digest_file(state_path,checkpoint)
            return
    except (OSError,ValueError,KeyError):
        pass
    generated={}

    def write(name, parts):
        path=local_path(root,name)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.partial')
        digest,size=hashlib.sha256(),0
        try:
            with temporary.open('wb') as output:
                for part in parts:
                    checkpoint()
                    output.write(part)
                    digest.update(part)
                    size+=len(part)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        generated[name]={'bytes':size,'sha256':digest.hexdigest()}

    message,parts=mime_parts(stream,record['begin'],record['end'],checkpoint)
    plain,rich,attachments,images,notes=[],[],[],{},[]
    for number,(part,start,end) in enumerate(parts):
        checkpoint()
        filename=part.get_filename()
        cid=str(part.get('Content-ID','')).strip('<> \t\r\n')
        encoding=str(part.get('Content-Transfer-Encoding',''))
        payload=decoded(stream,start,end,encoding,checkpoint)
        is_text=part.get_content_type() in ('text/plain','text/html')
        if filename or part.get_content_disposition()=='attachment' or not is_text:
            filename=header_text(filename) or ('message.eml' if part.get_content_type()=='message/rfc822' else 'attachment.bin')
            name=f'attachments/{number}-{safe_name(filename,80)}'
            write(directory+'/'+name,payload)
            attachments.append((filename,name))
            if cid and part.get_content_type() in ('image/png','image/jpeg','image/gif','image/webp'):
                images[cid]=quote(name,safe='/')
        else:
            data=bytearray()
            for block in payload:
                remaining=TEXT_LIMIT-len(data)
                data.extend(block[:remaining])
                if len(block)>remaining:
                    notes.append('매우 큰 본문은 처음 8 MiB까지 표시합니다. 전체 내용은 원본 MBOX에 보존됩니다.')
                    break
            text=decode_bytes(bytes(data),part.get_content_charset())
            (rich if part.get_content_type()=='text/html' else plain).append(text)
    content='\n'.join(safe_html(text,images) for text in rich) if rich else '<pre>'+html.escape('\n'.join(plain) or '본문이 없는 메일입니다.')+'</pre>'
    attachment_html=''.join('<li><a class="attachment" href="'+html.escape(quote(name,safe='/'),quote=True)+'">'+html.escape(filename)+'</a></li>' for filename,name in attachments)
    extra=''.join('<p>'+html.escape(note)+'</p>' for note in dict.fromkeys(notes))
    title=html.escape(record['title'])
    page=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{title}</title><style>body{{font:12px 'Malgun Gothic',sans-serif;line-height:1.6;margin:24px;overflow-wrap:anywhere}}h1{{font-size:18px}}pre{{white-space:pre-wrap;font:inherit}}table{{border-collapse:collapse;max-width:100%}}td,th{{border:1px solid #bbb;padding:6px}}img{{max-width:100%;height:auto}}a{{color:#075b9c}}</style>
<p><a href="../../../index.html#mail">메일 목록</a> · <a href="../../../{html.escape(quote(record['source'],safe='/'),quote=True)}">원본 메일 파일</a></p>
<h1>{title}</h1><p>보낸 사람: {html.escape(record['sender'])}<br>받는 사람: {html.escape(record['receiver'])}<br>참조: {html.escape(header_text(message.get('Cc')))}<br>일시: {html.escape(record['date'])}</p>
<main>{content}</main>{extra}<h2>첨부</h2><ul>{attachment_html or '<li>첨부 파일 없음</li>'}</ul></html>'''
    write(directory+'/index.html',[page.encode('utf-8')])
    atomic_write(state_path,json.dumps({'source':fingerprint,'files':generated},ensure_ascii=False).encode('utf-8'))
    generated[directory+'/state.json']=digest_file(state_path,checkpoint)
    manifest['files'].update(generated)


def build_portable_mail(root: Path, manifest=None, checkpoint=lambda:None, log=lambda _:None):
    root=root.resolve()
    if manifest is None:
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('viewer')!='sqlite-mail/1':
        return manifest
    if manifest.get('portable_mail',{}).get('version')==VERSION:
        return manifest
    from .db_client import safe_member
    count=0
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        total=db.execute('SELECT count(*) FROM mail_records').fetchone()[0]
        rows=db.execute('SELECT m.*,i.title FROM mail_records m JOIN items i ON m.id=i.id ORDER BY m.source,m.begin')
        with SourceReader() as opened:
            for row in rows:
                checkpoint()
                record=dict(row)
                name=record['source']
                safe_member(name)
                if not name.startswith('mail/raw/') or name not in manifest['files']:
                    raise CollectionError('검증되지 않은 메일 원본 경로입니다.')
                path=local_path(root,name)
                if path.stat().st_size!=manifest['files'][name]['bytes']:
                    raise CollectionError('메일 원본 파일의 크기가 변경되었습니다.')
                if not 0<=record['begin']<=record['end']<=path.stat().st_size:
                    raise CollectionError('메일 원본의 위치 또는 크기가 일치하지 않습니다.')
                stream=opened.open(path)
                render_record(root,manifest,stream,record,checkpoint)
                count+=1
                if count%100==0 or count==total:
                    log(f'메일 바로보기 생성 {count:,} / {total:,}건')
        if count!=total:
            raise CollectionError('메일 원본 위치 기록과 목록의 건수가 일치하지 않습니다.')
    manifest['portable_mail']={'version':VERSION,'count':count,'text_limit':TEXT_LIMIT}
    old_warning='메일은 제목·보낸 사람·받는 사람을 검색합니다. 본문·첨부는 원본에서 열람할 때 읽습니다.'
    manifest['warnings']=[value for value in manifest.get('warnings',[]) if value!=old_warning]
    for entry in manifest.get('coverage',[]):
        if entry.get('kind')=='mail_index':
            entry['body']='static_html_and_attachments'
    atomic_write(root/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
    return manifest


class SourceReader:
    def __enter__(self):
        self.path,self.stream=None,None
        return self

    def open(self,path):
        if self.path!=path:
            if self.stream:
                self.stream.close()
            self.path,self.stream=path,path.open('rb')
        return self.stream

    def __exit__(self,*_):
        if self.stream:
            self.stream.close()


def prepare_archive(root, checkpoint=lambda:None, log=lambda _:None):
    """One-time local conversion of an existing SSH backup, no server access."""
    root=Path(root).resolve()
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('viewer')=='local-db/1' or (manifest.get('format')=='segio-db-html/1' and manifest.get('viewer')!='sqlite-mail/1' and (root/'catalog.sqlite3').is_file()):
        from .search_page import install_search_page
        return install_search_page(root,checkpoint=checkpoint)
    if manifest.get('viewer')!='sqlite-mail/1':
        return manifest
    expected=manifest['files'].get('index.html')
    if expected and digest_file(root/'index.html',checkpoint)!=expected:
        raise CollectionError('index.html이 변경되었습니다. 파일 무결성을 확인하세요.')
    if digest_file(root/'catalog.sqlite3',checkpoint)!=manifest['files'].get('catalog.sqlite3'):
        raise CollectionError('메일 검색 DB가 변경되었습니다. 파일 무결성을 확인하세요.')
    if manifest.get('portable_mail',{}).get('version')==VERSION:
        from .search_page import install_search_page
        return install_search_page(root,checkpoint=checkpoint)
    manifest=build_portable_mail(root,manifest,checkpoint,log)
    from .search_page import install_search_page
    return install_search_page(root,manifest,checkpoint)
