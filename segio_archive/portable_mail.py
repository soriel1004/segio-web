"""Render local MBOX records to static HTML and attachments with bounded reads."""
from contextlib import closing
from email import policy
from email.parser import BytesHeaderParser
from email.errors import MessageError
import base64
import binascii
import hashlib
import html
import json
import quopri
import re
import sqlite3
import shutil
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
from bs4.exceptions import ParserRejectedMarkup

from .archive import atomic_write, safe_name
from .compact_mail import digest_file
from .mail_import import decode_bytes, header_text
from .web import CollectionError
from .attachment_safety import AttachmentStatus, AttachmentUnavailable, attachment_io, attachment_name, security_error, ISSUES as ATTACHMENT_ISSUES

VERSION = 'static-mail/1'
BLOCK = 65536
TEXT_LIMIT = 8 * 1024 * 1024
ISSUES = {
    'header_encoding':'메일 헤더 인코딩을 해석하지 못해 해당 값을 원문으로 표시했습니다.',
    'header_unicode':'잘못된 헤더 문자를 이스케이프해 표시했습니다.',
    'invalid_base64':'메일 본문 또는 첨부의 Base64 데이터가 올바르지 않습니다.',
    'mime_structure':'메일 MIME 구조 또는 구분자를 해석하지 못했습니다.',
    'mime_limit':'메일 MIME 헤더·중첩·부분 수가 안전 처리 한도를 넘었습니다.',
    'transfer_encoding':'지원하지 않는 메일 전송 인코딩입니다.',
    'html_as_text':'메일 HTML을 해석하지 못해 HTML 원문을 텍스트로 표시했습니다.',
}
ISSUES.update(ATTACHMENT_ISSUES)
ISSUE_REPORT='mail/processing-issues.html'


class MailFormatError(CollectionError):
    def __init__(self, code):
        self.issue=code
        super().__init__(ISSUES[code])


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
            raise MailFormatError('mime_limit')
    return BytesHeaderParser(policy=policy.compat32).parsebytes(bytes(data)), stream.tell()


def mime_parts(stream, start, end, checkpoint, depth=0, budget=None):
    """Find MIME leaves by offsets, never loading a full attachment/message."""
    if depth > 20:
        raise MailFormatError('mime_limit')
    budget = budget if budget is not None else {'headers':0}
    message, body = headers(stream,start,end)
    budget['headers'] += body-start
    if budget['headers'] > 8*1024*1024:
        raise MailFormatError('mime_limit')
    if message.get_content_maintype() != 'multipart':
        return message, [(message,body,end)]
    boundary = message.get_boundary()
    if not boundary or len(boundary)>200:
        raise MailFormatError('mime_structure')
    try:
        marker = b'--'+boundary.encode('ascii')
    except UnicodeError:
        raise MailFormatError('mime_structure') from None
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
                raise MailFormatError('mime_limit')
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
        _,children=mime_parts(stream,after,stop,checkpoint,depth+1,budget)
        leaves.extend(children)
        if len(leaves)>10000:
            raise MailFormatError('mime_limit')
    if not delimiters:
        raise MailFormatError('mime_structure')
    return message,leaves


def decoded(stream, start, end, encoding, checkpoint):
    encoding=encoding.lower().strip()
    carry=b''
    padded=False
    for block in chunks(stream,start,end,checkpoint):
        if encoding=='base64':
            block=re.sub(rb'[ \t\r\n\f\v]',b'',block)
            if re.search(rb'[^A-Za-z0-9+/=]',block) or (padded and block):
                raise MailFormatError('invalid_base64')
            block=carry+block
            size=len(block)//4*4
            carry=block[size:]
            if size:
                try:
                    value=base64.b64decode(block[:size],validate=True)
                except binascii.Error:
                    raise MailFormatError('invalid_base64') from None
                padded=b'=' in block[:size]
                if padded and carry:
                    raise MailFormatError('invalid_base64')
                yield value
        elif encoding=='quoted-printable':
            block=carry+block
            split=block.rfind(b'=',max(0,len(block)-2))
            carry=block[split:] if split>=0 else b''
            yield quopri.decodestring(block[:split] if split>=0 else block)
        elif encoding in ('','7bit','8bit','binary'):
            yield block
        else:
            raise MailFormatError('transfer_encoding')
    if carry:
        try:
            value=base64.b64decode(carry+b'='*((-len(carry))%4),validate=True) if encoding=='base64' else quopri.decodestring(carry)
        except binascii.Error:
            raise MailFormatError('invalid_base64') from None
        yield value


def safe_html(text, images, issues=None):
    try:
        return sanitized_html(text,images)
    except (ParserRejectedMarkup,RecursionError,ValueError):
        if issues is not None:issues.add('html_as_text')
        return '<pre>'+html.escape(text)+'</pre>'


def sanitized_html(text, images):
    soup=BeautifulSoup(text,'html.parser')
    for tag in soup.select('script,style,iframe,object,embed,svg,math,form,template,head'):
        if tag.parent is not None:tag.decompose()
    allowed=set('p br div span table thead tbody tfoot tr td th ul ol li strong em b i u s hr blockquote pre code h1 h2 h3 h4 h5 h6 img a'.split())
    for tag in list(soup.find_all(True)):
        # Removing a malformed parent can also decompose descendants already
        # present in this snapshot. unwrap() rejects detached elements.
        if tag.parent is None or tag.name is None:
            continue
        if tag.name not in allowed:
            tag.unwrap()
            continue
        old=dict(tag.attrs)
        tag.attrs={}
        if tag.name=='img':
            source=old.get('src','')
            if not isinstance(source,str):
                tag.decompose()
                continue
            image=images.get(source[4:].strip('<>')) if source.lower().startswith('cid:') else None
            if not image:
                tag.decompose()
                continue
            tag['src']=image
            tag['alt']=old.get('alt','본문 이미지')
        if tag.name in ('td','th'):
            for name in ('colspan','rowspan'):
                value=str(old.get(name,''))
                if re.fullmatch(r'[0-9]{1,4}',value):
                    tag[name]=str(min(1000,int(value)))
    return str(soup)


def write_generated(root, name, parts, generated, checkpoint):
    from .storage import require_space
    path=local_path(root,name)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.partial')
    digest,size=hashlib.sha256(),0
    next_space_check=0
    try:
        output=attachment_io(lambda:temporary.open('wb'),name)
        try:
            for part in parts:
                checkpoint()
                if size>=next_space_check:
                    require_space(root,len(part),'메일 바로보기')
                    next_space_check=size+64*1024*1024
                attachment_io(lambda:output.write(part),name)
                digest.update(part)
                size+=len(part)
        except BaseException:
            # A secondary close error must not downgrade a raw-source read
            # failure, cancellation or disk error to an attachment warning.
            try:output.close()
            except OSError:pass
            raise
        else:
            attachment_io(output.close,name)
        from .file_ops import replace_file
        attachment_io(lambda:replace_file(temporary,path,checkpoint),name,
                      missing=temporary.parent.is_dir() and path.parent.is_dir())
        actual=attachment_io(path.stat,name,missing=True)
        if attachment_name(name) and actual.st_size!=size:
            raise AttachmentUnavailable('attachment_unavailable')
    finally:
        try:temporary.unlink(missing_ok=True)
        except OSError:pass
    generated[name]={'bytes':size,'sha256':digest.hexdigest()}


def render_record(root, manifest, stream, record, checkpoint):
    try:
        return render_message(root,manifest,stream,record,checkpoint)
    except MailFormatError as exc:
        return render_raw_record(root,manifest,stream,record,checkpoint,exc.issue)


def render_message(root, manifest, stream, record, checkpoint):
    directory=Path(page_path(record['id'])).parent.as_posix()
    previous_attachments=(local_path(root,directory)/'attachments').is_dir()
    cache_matched=False
    local_path(root,directory).mkdir(parents=True,exist_ok=True)
    source_info=manifest['files'][record['source']]
    fingerprint=[VERSION,record['source'],record['begin'],record['end'],source_info['sha256']]
    status=AttachmentStatus(root,directory,fingerprint)
    state_path=local_path(root,directory+'/state.json')
    try:
        state=json.loads(state_path.read_text(encoding='utf-8'))
        codes=state.get('issues',[])
        if (not isinstance(codes,list) or any(code not in ISSUES for code in codes) or
                (state.get('raw_only') is True and not codes)):
            raise ValueError('Invalid derived mail state')
        if (state['source']==fingerprint and directory+'/index.html' in state['files']
                and all(name.startswith(directory+'/') for name in state['files'])):
            cache_matched=True
            valid=True
            for name,value in state['files'].items():
                checkpoint()
                if name in status.skipped:
                    valid=False
                    continue
                try:
                    actual=attachment_io(lambda:digest_file(local_path(root,name),checkpoint),name,missing=True)
                    if actual!=value:
                        if attachment_name(name):status.record(name,'attachment_unavailable')
                        valid=False
                except AttachmentUnavailable as exc:
                    status.record(name,exc.issue)
                    valid=False
                except FileNotFoundError:
                    if name.endswith('/original.eml'):
                        raise CollectionError('보존했던 원본 EML이 없어졌습니다. 보안 프로그램의 격리·삭제 기록을 확인하세요. 자동 재생성을 중단했습니다.') from None
                    valid=False
            if valid:
                manifest['files'].update(state['files'])
                manifest['files'][directory+'/state.json']=digest_file(state_path,checkpoint)
                return {'issues':codes,'raw_only':state.get('raw_only') is True,
                        'source_only':state.get('source_only') is True,'unavailable_attachments':len(status.skipped)}
    except OSError as exc:
        if security_error(exc):raise
    except (ValueError,KeyError,TypeError,AttributeError):
        pass
    generated={}

    def write(name, parts):
        write_generated(root,name,parts,generated,checkpoint)

    try:
        message,parts=mime_parts(stream,record['begin'],record['end'],checkpoint)
    except (MessageError,ValueError,LookupError):
        raise MailFormatError('mime_structure') from None
    issues=set(status.skipped.values())
    for field in ('Subject','From','To','Date','Cc'):
        header_text(message.get(field),issues)
    plain,rich,attachments,images,notes,unavailable=[],[],[],{},[],[]
    text_bytes = 0
    for number,(part,start,end) in enumerate(parts):
        checkpoint()
        try:
            filename=part.get_filename()
        except (MessageError,ValueError,LookupError):
            raise MailFormatError('mime_structure') from None
        cid=str(part.get('Content-ID','')).strip('<> \t\r\n')
        encoding=str(part.get('Content-Transfer-Encoding',''))
        payload=decoded(stream,start,end,encoding,checkpoint)
        is_text=part.get_content_type() in ('text/plain','text/html')
        if filename or part.get_content_disposition()=='attachment' or not is_text:
            filename=header_text(filename,issues) or ('message.eml' if part.get_content_type()=='message/rfc822' else 'attachment.bin')
            name=f'attachments/{number}-{safe_name(filename,80)}'
            relative=directory+'/'+name
            if relative in status.skipped:
                unavailable.append(filename)
                continue
            try:
                if previous_attachments and not cache_matched:
                    # Old releases could stop after writing attachments but
                    # before saving state.json. Do not recreate a disappeared
                    # attachment from that incomplete attempt. Verify/reuse only
                    # the bytes still present, without rewriting them.
                    target=local_path(root,relative)
                    if not target.exists():
                        raise AttachmentUnavailable('attachment_unverified')
                    digest,size=hashlib.sha256(),0
                    for block in payload:
                        checkpoint()
                        digest.update(block)
                        size+=len(block)
                    expected={'bytes':size,'sha256':digest.hexdigest()}
                    actual=attachment_io(lambda:digest_file(target,checkpoint),relative,missing=True)
                    if actual!=expected:
                        raise AttachmentUnavailable('attachment_unverified')
                    generated[relative]=expected
                else:
                    write(relative,payload)
            except AttachmentUnavailable as exc:
                status.record(relative,exc.issue)
                issues.add(exc.issue)
                unavailable.append(filename)
                continue
            attachments.append((filename,name))
            if cid and part.get_content_type() in ('image/png','image/jpeg','image/gif','image/webp'):
                images[cid]=quote(name,safe='/')
        else:
            data=bytearray()
            for block in payload:
                remaining=max(0,TEXT_LIMIT-text_bytes-len(data))
                data.extend(block[:remaining])
                if len(block)>remaining:
                    notes.append('매우 큰 본문은 처음 8 MiB까지 표시합니다. 전체 내용은 원본 MBOX에 보존됩니다.')
                    break
            text=decode_bytes(bytes(data),part.get_content_charset())
            text_bytes += len(data)
            if text:
                (rich if part.get_content_type()=='text/html' else plain).append(text)
    # Reconcile files removed after creation while other MIME parts were read.
    available=[]
    for filename,name in attachments:
        relative=directory+'/'+name
        try:
            actual=attachment_io(local_path(root,relative).stat,relative,missing=True)
            if actual.st_size!=generated[relative]['bytes']:
                raise AttachmentUnavailable('attachment_unavailable')
            available.append((filename,name))
        except AttachmentUnavailable as exc:
            status.record(relative,exc.issue)
            issues.add(exc.issue)
            unavailable.append(filename)
            generated.pop(relative,None)
            images={cid:src for cid,src in images.items() if src!=quote(name,safe='/')}
    attachments=available
    content='\n'.join(safe_html(text,images,issues) for text in rich) if rich else '<pre>'+html.escape('\n'.join(plain) or '본문이 없는 메일입니다.')+'</pre>'
    notes.extend(ISSUES[code] for code in sorted(issues))
    attachment_html=''.join('<li><a class="attachment" target="_blank" rel="noopener noreferrer" href="'+html.escape(quote(name,safe='/'),quote=True)+'">'+html.escape(filename)+'</a></li>' for filename,name in attachments)
    attachment_html+=''.join('<li class="unavailable">'+html.escape(filename)+' — 첨부 미확보 · 자동 재추출하지 않음</li>' for filename in unavailable)
    extra=''.join('<p>'+html.escape(note)+'</p>' for note in dict.fromkeys(notes))
    title=html.escape(record['title'])
    page=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{title}</title><style>body{{font:12px 'Malgun Gothic',sans-serif;line-height:1.6;margin:24px;overflow-wrap:anywhere}}h1{{font-size:18px}}pre{{white-space:pre-wrap;font:inherit}}table{{border-collapse:collapse;max-width:100%}}td,th{{border:1px solid #bbb;padding:6px}}img{{max-width:100%;height:auto}}a{{color:#075b9c}}</style>
<p><a href="../../../index.html#mail">메일 목록</a> · <a href="../../../{html.escape(quote(record['source'],safe='/'),quote=True)}">원본 메일 파일</a></p>
<h1>{title}</h1><p>보낸 사람: {html.escape(record['sender'])}<br>받는 사람: {html.escape(record['receiver'])}<br>참조: {html.escape(header_text(message.get('Cc')))}<br>일시: {html.escape(record['date'])}</p>
<main>{content}</main>{extra}<h2>첨부</h2><ul>{attachment_html or '<li>첨부 파일 없음</li>'}</ul></html>'''
    write(directory+'/index.html',[page.encode('utf-8')])
    for name in status.skipped:manifest['files'].pop(name,None)
    if status.path.exists():
        generated[directory+'/attachment-status.json']=digest_file(status.path,checkpoint)
    atomic_write(state_path,json.dumps({'source':fingerprint,'files':generated,'issues':sorted(issues),'raw_only':False,
                                       'unavailable_attachments':len(status.skipped)},ensure_ascii=False).encode('utf-8'))
    generated[directory+'/state.json']=digest_file(state_path,checkpoint)
    manifest['files'].update(generated)
    return {'issues':sorted(issues),'raw_only':False,'unavailable_attachments':len(status.skipped)}


def render_raw_record(root,manifest,stream,record,checkpoint,issue):
    """Keep an undecodable message exact; never publish partial attachments."""
    directory=Path(page_path(record['id'])).parent.as_posix()
    fingerprint=[VERSION,record['source'],record['begin'],record['end'],manifest['files'][record['source']]['sha256']]
    status=AttachmentStatus(root,directory,fingerprint)
    if status.skipped:
        # An EML copy would recreate the content of a security-blocked
        # attachment. Keep the existing source only and emit an inert notice.
        codes=sorted({issue,*status.skipped.values()})
        generated={}
        page=('<!doctype html><html lang="ko"><meta charset="utf-8">'
              '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
              '<title>'+html.escape(record['title'])+'</title><a href="../../../index.html#mail">메일 목록</a>'
              '<h1>'+html.escape(record['title'])+'</h1><p>본문·첨부 자동 변환을 완료하지 못했습니다. '
              '첨부 미확보 기록이 있어 별도 EML과 차단된 첨부를 다시 생성하지 않았습니다. '
              '기존 원본 자료와 보안 프로그램의 보호 기록을 확인하세요.</p><ul>'+
              ''.join('<li>'+ISSUES[code]+'</li>' for code in codes)+'</ul></html>')
        write_generated(root,directory+'/index.html',[page.encode()],generated,checkpoint)
        generated[directory+'/attachment-status.json']=digest_file(status.path,checkpoint)
        for name in status.skipped:manifest['files'].pop(name,None)
        state={'source':fingerprint,'files':generated,'issues':codes,'raw_only':False,'source_only':True,
               'unavailable_attachments':len(status.skipped)}
        state_path=root/directory/'state.json'
        atomic_write(state_path,json.dumps(state,ensure_ascii=False).encode())
        generated[directory+'/state.json']=digest_file(state_path,checkpoint)
        manifest['files'].update(generated)
        return {'issues':codes,'raw_only':False,'source_only':True,'unavailable_attachments':len(status.skipped)}
    path=local_path(root,directory)
    for current in (path,*path.parents):
        if current==root:break
        if current.is_symlink():
            raise CollectionError('메일 바로보기 경로의 심볼릭 링크는 지원하지 않습니다.')
    else:
        raise CollectionError('메일 바로보기 경로가 보관 폴더를 벗어납니다.')
    checkpoint()
    if path.exists():shutil.rmtree(path)
    for name in list(manifest['files']):
        if name.startswith(directory+'/'):manifest['files'].pop(name)
    generated={}
    write_generated(root,directory+'/original.eml',
        chunks(stream,record['begin'],record['end'],checkpoint),generated,checkpoint)
    title=html.escape(record['title'])
    page=('<!doctype html><html lang="ko"><meta charset="utf-8">'
          '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
          '<title>'+title+'</title><style>body{font:12px sans-serif;margin:24px;overflow-wrap:anywhere}.note{background:#fff1cb;padding:16px}</style>'
          '<a href="../../../index.html#mail">메일 목록</a><h1>'+title+'</h1><p class="note">'
          +ISSUES[issue]+' 이 메일의 본문·첨부 자동 변환은 완료하지 못했습니다. 원본은 그대로 보존했습니다.</p>'
          '<p><a href="original.eml" target="_blank" rel="noopener noreferrer">원본 EML 열기·저장</a></p></html>')
    write_generated(root,directory+'/index.html',[page.encode('utf-8')],generated,checkpoint)
    state={'source':[VERSION,record['source'],record['begin'],record['end'],manifest['files'][record['source']]['sha256']],
           'files':generated,'issues':[issue],'raw_only':True}
    state_path=path/'state.json'
    atomic_write(state_path,json.dumps(state,ensure_ascii=False).encode('utf-8'))
    generated[directory+'/state.json']=digest_file(state_path,checkpoint)
    manifest['files'].update(generated)
    return {'issues':[issue],'raw_only':True}


class IssueReport:
    """Stream a local clickable report without retaining a mailbox-sized list."""
    def __init__(self,root,manifest,checkpoint):
        self.root,self.manifest,self.checkpoint=root,manifest,checkpoint
        self.path=local_path(root,ISSUE_REPORT)
        self.temporary=self.path.with_suffix('.html.partial')
        self.count,self.raw_only,self.source_only,self.unavailable=0,0,0,0

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.output=self.temporary.open('wb')
        header=('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
            '<title>메일 변환 확인 사항</title><style>body{font:12px sans-serif;margin:24px}li{margin:12px 0}</style>'
            '<a href="../index.html#mail">메일 목록</a><h1>메일 변환 확인 사항</h1>'
            '<p>원본 메일은 보존했습니다. 아래 항목의 본문·첨부를 확인하세요.</p><ol>').encode('utf-8')
        try:self.output.write(header)
        except BaseException:
            self.output.close()
            try:self.temporary.unlink(missing_ok=True)
            except OSError:pass
            raise
        return self

    def add(self,record,result):
        codes=sorted({code for code in result.get('issues',[]) if code in ISSUES})
        if not codes:return
        self.count+=1
        self.raw_only+=int(result.get('raw_only') is True)
        self.source_only+=int(result.get('source_only') is True)
        unavailable=result.get('unavailable_attachments',0)
        self.unavailable+=unavailable if type(unavailable) is int and unavailable>=0 else 0
        link=page_path(record['id'])[len('mail/'):]
        body='<li><a href="'+link+'">'+html.escape(record['title'])+'</a><ul>'
        body+=''.join('<li>'+ISSUES[code]+'</li>' for code in codes)+'</ul></li>'
        self.output.write(body.encode('utf-8'))

    def __exit__(self,kind,value,trace):
        try:
            if kind is None:self.output.write('</ol></html>'.encode())
            self.output.close()
            if kind is None:
                if self.count:
                    from .file_ops import replace_file
                    replace_file(self.temporary,self.path,self.checkpoint)
                    self.manifest['files'][ISSUE_REPORT]=digest_file(self.path,self.checkpoint)
                else:
                    self.path.unlink(missing_ok=True)
                    self.manifest['files'].pop(ISSUE_REPORT,None)
        finally:
            try:self.temporary.unlink(missing_ok=True)
            except OSError:pass


def retained_result(root, manifest, record, checkpoint):
    """Reuse a completed page without rereading old mail/attachment payloads."""
    path = Path(page_path(record['id'])).parent/'state.json'
    state_path = local_path(root,path.as_posix())
    if state_path.stat().st_size > 4*1024*1024 or digest_file(state_path,checkpoint) != manifest['files'].get(path.as_posix()):
        raise CollectionError('이전 메일 바로보기 검증 정보가 변경되었습니다. 먼저 기존 백업 갱신을 실행하세요.')
    state = json.loads(state_path.read_bytes())
    fingerprint = [VERSION,record['source'],record['begin'],record['end'],manifest['files'][record['source']]['sha256']]
    codes = state.get('issues',[])
    if (state.get('source') != fingerprint or not isinstance(codes,list) or any(code not in ISSUES for code in codes) or
            (state.get('raw_only') is True and not codes) or
            page_path(record['id']) not in state.get('files',{}) or
            any(value != manifest['files'].get(name) for name,value in state['files'].items())):
        raise CollectionError('이전 메일 바로보기와 원본 기록이 일치하지 않습니다. 먼저 기존 백업 갱신을 실행하세요.')
    for name,expected in state['files'].items():
        actual=attachment_io(local_path(root,name).stat,name,missing=True)
        if actual.st_size!=expected['bytes']:
            if attachment_name(name):
                raise AttachmentUnavailable('attachment_unavailable')
            raise CollectionError('기존 메일 바로보기 파일의 크기가 변경되었습니다. 파일 무결성을 확인하세요.')
    status=AttachmentStatus(root,path.parent.as_posix(),fingerprint)
    return {'issues':codes,'raw_only':state.get('raw_only') is True,
            'source_only':state.get('source_only') is True,'unavailable_attachments':len(status.skipped)}


def build_portable_mail(root: Path, manifest=None, checkpoint=lambda:None, log=lambda _:None, progress=None, *, changed_sources=None, audit=False):
    root=root.resolve()
    if manifest is None:
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('viewer')!='sqlite-mail/1':
        return manifest
    if manifest.get('portable_mail',{}).get('version')==VERSION and not audit:
        return manifest
    from .db_client import safe_member
    count,generated=0,0
    report = progress or (lambda *args, **kwargs: None)
    with IssueReport(root,manifest,checkpoint) as issues, closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        total=db.execute('SELECT count(*) FROM mail_records').fetchone()[0]
        target_total = total if changed_sources is None else sum(count for name,count in db.execute('SELECT source,count(*) FROM mail_records GROUP BY source') if name in changed_sources)
        report('attachment_audit' if audit else 'render', 0, total if audit else target_total, unit='messages')
        rows=db.execute('SELECT m.*,i.title FROM mail_records m JOIN items i ON m.id=i.id ORDER BY m.source,m.begin')
        with closing(rows), SourceReader() as opened:
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
                if changed_sources is not None and name not in changed_sources:
                    try:
                        result=retained_result(root,manifest,record,checkpoint)
                    except AttachmentUnavailable:
                        stream=opened.open(path)
                        result=render_record(root,manifest,stream,record,checkpoint)
                        generated+=1
                else:
                    stream=opened.open(path)
                    result=render_record(root,manifest,stream,record,checkpoint)
                    generated+=1
                    report('render', generated, target_total, unit='messages')
                issues.add(record,result)
                count+=1
                if audit:report('attachment_audit',count,total,unit='messages')
                if count%100==0 or count==total:
                    log(f'메일 첨부 상태 확인 {count:,} / {total:,}건 · 미확보 보완 {generated:,}건' if audit else
                        f'추가·변경 메일 바로보기 생성 {generated:,} / {target_total:,}건 · 기존 메일 유지' if changed_sources is not None else f'메일 바로보기 생성 {count:,} / {total:,}건')
        if count!=total:
            raise CollectionError('메일 원본 위치 기록과 목록의 건수가 일치하지 않습니다.')
    manifest['portable_mail']={'version':VERSION,'count':count,'text_limit':TEXT_LIMIT,
                               'warning_count':issues.count,'raw_only_count':issues.raw_only,
                               'source_only_count':issues.source_only,'unavailable_attachment_count':issues.unavailable}
    if issues.count:
        manifest['portable_mail']['issues_path']=ISSUE_REPORT
        manifest['state']='collected_with_warnings'
        log('메일 바로보기 확인 필요 %d건 · 원본 보존 · mail/processing-issues.html에서 확인하세요.'%issues.count)
    old_warning='메일은 제목·보낸 사람·받는 사람을 검색합니다. 본문·첨부는 원본에서 열람할 때 읽습니다.'
    manifest['warnings']=[value for value in manifest.get('warnings',[]) if value!=old_warning]
    manifest['warnings']=[value for value in manifest['warnings'] if not value.startswith('메일 바로보기 확인 필요:')]
    if issues.count:
        manifest['warnings'].append('메일 바로보기 확인 필요: %d건 (원본 EML만 보존한 메일 %d건, 첨부 미확보 %d개). mail/processing-issues.html을 확인하세요.'%(issues.count,issues.raw_only,issues.unavailable))
    for entry in manifest.get('coverage',[]):
        if entry.get('kind')=='mail_index':
            entry['body']='static_html_and_attachments'
    atomic_write(root/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
    if not audit:
        return build_portable_mail(root,manifest,checkpoint,log,progress,changed_sources=set(),audit=True)
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


def prepare_archive(root, checkpoint=lambda:None, log=lambda _:None, progress=None):
    """One-time local conversion of an existing SSH backup, no server access."""
    root=Path(root).resolve()
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if isinstance(manifest.get('live_export'),dict) and (
            manifest.get('local_processing',{}).get('state') == 'pending' or
            not (root/'catalog.sqlite3').is_file()):
        from .mail_import import build_local_catalog
        return build_local_catalog(root,manifest['user'],checkpoint,log,progress)
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
        manifest=build_portable_mail(root,manifest,checkpoint,log,progress,changed_sources=set(),audit=True)
        from .search_page import install_search_page
        return install_search_page(root,manifest,checkpoint,progress)
    manifest=build_portable_mail(root,manifest,checkpoint,log,progress)
    from .search_page import install_search_page
    return install_search_page(root,manifest,checkpoint)
