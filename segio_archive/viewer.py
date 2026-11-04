"""Local-only archive viewer, also usable where file:// is disabled by browser policy."""
from __future__ import annotations

import mimetypes
import json
import re
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs, quote


class Viewer:
    def __init__(self, folder: Path, *, bridge_token=None, port=0, bridge=False):
        if bridge_token is not None and not re.fullmatch('[a-f0-9]{64}',bridge_token):raise ValueError('PC 관리 연결 토큰이 올바르지 않습니다.')
        self.folder = folder.resolve()
        if not (self.folder / "index.html").is_file():
            raise ValueError("보관함 index.html이 없습니다.")
        self.edit_enabled=False
        self.account=''
        try:
            manifest = json.loads((self.folder/'manifest.json').read_text(encoding='utf-8'))
            self.account=str(manifest.get('user',''))
            self.edit_enabled=(manifest.get('format')=='segio-db-html/1' and isinstance(manifest.get('live_export'),dict) and
                               manifest.get('state') in ('collected','collected_with_warnings') and
                               manifest.get('local_processing',{}).get('state')!='pending')
            self.compact = (manifest.get('viewer') in ('sqlite-mail/1','local-db/1') or
                            manifest.get('format')=='segio-db-html/1' and (self.folder/'catalog.sqlite3').is_file())
        except (OSError, ValueError):
            self.compact = False
        if self.compact:
            from .search_page import install_search_page
            install_search_page(self.folder)
        self.token = secrets.token_urlsafe(24)
        self.bridge_token=bridge_token or secrets.token_hex(32)
        self.mutation_token=secrets.token_urlsafe(32)
        self.bridge=bridge
        self.last_activity=time.monotonic()
        from .delete_service import DeleteService
        self.edits=DeleteService(self.folder)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.serve(False)

            def do_HEAD(self):
                self.serve(True)

            def bridge_path(self):
                return (owner.bridge and self.headers.get('Host')==f'127.0.0.1:{owner.server.server_port}' and
                        urlsplit(self.path).path.startswith('/'+owner.bridge_token+'/bridge/') and
                        self.headers.get('Origin')==f'http://127.0.0.1:{owner.server.server_port}' and
                        self.headers.get('X-Bridge-Token')==owner.bridge_token)

            def do_OPTIONS(self):
                return self.send_error(403)

            def edit_reply(self,data,bridge=False):
                data=json.dumps(data,ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cross-Origin-Resource-Policy','same-origin')
                self.end_headers()
                if self.command=='HEAD':return
                try:self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass

            def do_POST(self):
                host=f'127.0.0.1:{owner.server.server_port}'
                prefix='/'+owner.token+'/'
                path=urlsplit(self.path).path
                if self.headers.get('Host')!=host:return self.send_error(403)
                bridge=self.bridge_path()
                if bridge:prefix='/'+owner.bridge_token+'/'
                if not path.startswith(prefix):return self.send_error(403)
                action=path[len(prefix):]
                if bridge:
                    if action not in ('bridge/create','bridge/add','bridge/open'):return self.send_error(403)
                elif (self.headers.get('Origin')!='http://'+host or
                        not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',self.headers.get('X-Archive-Token','')) or
                        not secrets.compare_digest(self.headers.get('X-Archive-Token',''),owner.mutation_token) or
                        not action.startswith('api/edit/')):
                    return self.send_error(403)
                if not owner.edit_enabled:return self.send_error(403)
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=262144 or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('요청 형식을 확인하세요.')
                    self.connection.settimeout(20)
                    fields=json.loads(self.rfile.read(length))
                    if not isinstance(fields,dict):raise ValueError('요청 형식을 확인하세요.')
                    owner.last_activity=time.monotonic()
                    selection=fields.get('selection')
                    if action in ('api/edit/create','bridge/create'):result=owner.edits.create(fields)
                    elif action in ('api/edit/add','bridge/add'):result=owner.edits.add(selection,fields.get('ids'))
                    elif action=='api/edit/preview':result=owner.edits.preview(selection)
                    elif action=='api/edit/start':result=owner.edits.start(selection,fields.get('clean_files',True))
                    elif action=='api/edit/cancel':result=owner.edits.cancel(selection)
                    elif action=='bridge/open':
                        owner.edits.get(selection)
                        owner.edits.initial=selection
                        result={'opened':True,'url':owner.url+'#delete'}
                    else:return self.send_error(404)
                    self.edit_reply(result,bridge)
                except Exception as exc:
                    from .web import CollectionError
                    message=str(exc) if isinstance(exc,(ValueError,CollectionError)) else '백업 폴더의 접근 상태·완료 상태를 확인하세요.'
                    self.edit_reply({'error':message},bridge)

            def serve(self, head):
                if self.headers.get("Host") != f"127.0.0.1:{owner.server.server_port}":
                    return self.send_error(403)
                prefix = "/" + owner.token + "/"
                name = unquote(urlsplit(self.path).path)
                if owner.bridge and urlsplit(self.path).path=='/'+owner.bridge_token+'/bridge/connect':
                    from .delete_bridge import page
                    data=page(owner.bridge_token)
                    self.send_response(200)
                    self.send_header('Content-Type','text/html; charset=utf-8')
                    self.send_header('Content-Length',str(len(data)))
                    self.send_header('Cache-Control','no-store')
                    self.send_header('Referrer-Policy','no-referrer')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                    self.end_headers()
                    if not head:self.wfile.write(data)
                    return
                if not name.startswith(prefix):
                    return self.send_error(404)
                if self.headers.get('Sec-Fetch-Site')=='cross-site':return self.send_error(403)
                relative_name = name[len(prefix):]
                owner.last_activity=time.monotonic()
                if relative_name=='api/edit/info':
                    initial=owner.edits.jobs.get(owner.edits.initial,{})
                    return self.edit_reply({'enabled':owner.edit_enabled,'token':owner.mutation_token,
                                            'initial':owner.edits.initial,'initial_filters':initial.get('filters',{}),
                                            'account':owner.account,'folder':owner.folder.name})
                if relative_name=='api/edit/status':
                    try:
                        fields=parse_qs(urlsplit(self.path).query)
                        return self.edit_reply(owner.edits.status(fields.get('selection',[''])[0]))
                    except ValueError:return self.send_error(404)
                if owner.compact and relative_name == 'api/search':
                    from .compact_mail import search
                    try:
                        fields = parse_qs(urlsplit(self.path).query)
                        data = search(owner.folder, fields.get('q',[''])[0], fields.get('kind',[''])[0],
                                      int(fields.get('page',['0'])[0]),fields.get('mailbox',[''])[0],fields.get('category',[''])[0],
                                      page_size=int(fields.get('page_size',['100'])[0]))
                        return self.payload(json.dumps(data,ensure_ascii=False).encode('utf-8'), 'application/json', head)
                    except Exception:
                        return self.send_error(400)
                match = re.fullmatch(r'mail/(mail-[0-9]{9})(?:/(original\.eml|attachment/([0-9]{1,4})))?', relative_name)
                if owner.compact and match:
                    from .compact_mail import mail_record, message_html, parse_message
                    try:
                        identity, action, number = match.groups()
                        if action == 'original.eml':
                            record, source = mail_record(owner.folder, identity)
                            size = record['end'] - record['begin']
                            self.headers_for('message/rfc822', size, identity+'.eml')
                            if not head:
                                with source.open('rb') as stream:
                                    stream.seek(record['begin'])
                                    while size:
                                        block = stream.read(min(size,1024*1024))
                                        if not block:
                                            break
                                        self.wfile.write(block)
                                        size -= len(block)
                            return
                        if number is not None:
                            record, message = parse_message(owner.folder, identity)
                            if message is None:
                                return self.send_error(404)
                            part = next((p for n,p in enumerate(message.walk()) if n==int(number)), None)
                            if part is None or part.is_multipart() or not (part.get_filename() or part.get_content_disposition()=='attachment'):
                                return self.send_error(404)
                            from .mail_import import header_text
                            return self.payload(part.get_payload(decode=True) or b'', 'application/octet-stream',
                                                head, header_text(part.get_filename()) or 'attachment.bin')
                        return self.payload(message_html(owner.folder,identity), 'text/html; charset=utf-8', head)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    except Exception:
                        return self.send_error(404)
                target = (owner.folder / name[len(prefix):]).resolve()
                if not target.is_relative_to(owner.folder) or not target.is_file():
                    return self.send_error(404)
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", mime + ("; charset=utf-8" if mime == "text/html" else ""))
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header('Cross-Origin-Resource-Policy','same-origin')
                relative = target.relative_to(owner.folder)
                if ("attachments" in relative.parts or target.suffix.lower() == ".eml" or
                        relative.parts[:2] == ('mail', 'raw')):
                    inline = ('attachments' in relative.parts and mime in (
                        'application/pdf', 'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'text/plain'))
                    self.send_header("Content-Disposition", ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(target.name))
                else:
                    script = " script-src 'self' 'unsafe-inline';" if target == owner.folder / "index.html" else ""
                    connect = " connect-src 'self';" if owner.compact and target == owner.folder/'index.html' else ''
                    images="img-src 'self' data:;" if relative.parts[:2]==('mail','offline') else 'img-src data:;'
                    self.send_header("Content-Security-Policy", "default-src 'none'; "+images+" style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none';" + script + connect)
                self.end_headers()
                if not head:
                    try:
                        with target.open('rb') as incoming:
                            shutil.copyfileobj(incoming, self.wfile, length=1024*1024)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def headers_for(self, mime, length, filename=None):
                self.send_response(200)
                self.send_header('Content-Type',mime)
                self.send_header('Content-Length',str(length))
                self.send_header('Cache-Control','no-store')
                self.send_header('Referrer-Policy','no-referrer')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cross-Origin-Resource-Policy','same-origin')
                self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
                if filename:
                    self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(filename))
                self.end_headers()

            def payload(self, data, mime, head, filename=None):
                self.headers_for(mime,len(data),filename)
                if not head:
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/{self.token}/index.html"

    def close(self):
        self.edits.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def edit_running(self):
        with self.edits.lock:return any(item['state']=='running' for item in self.edits.jobs.values())
