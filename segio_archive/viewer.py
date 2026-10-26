"""Local-only archive viewer, also usable where file:// is disabled by browser policy."""
from __future__ import annotations

import mimetypes
import json
import re
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs, quote


class Viewer:
    def __init__(self, folder: Path):
        self.folder = folder.resolve()
        if not (self.folder / "index.html").is_file():
            raise ValueError("보관함 index.html이 없습니다.")
        try:
            manifest = json.loads((self.folder/'manifest.json').read_text(encoding='utf-8'))
            self.compact = (manifest.get('viewer') in ('sqlite-mail/1','local-db/1') or
                            manifest.get('format')=='segio-db-html/1' and (self.folder/'catalog.sqlite3').is_file())
        except (OSError, ValueError):
            self.compact = False
        if self.compact:
            from .search_page import install_search_page
            install_search_page(self.folder)
        self.token = secrets.token_urlsafe(24)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.serve(False)

            def do_HEAD(self):
                self.serve(True)

            def serve(self, head):
                if self.headers.get("Host") != f"127.0.0.1:{owner.server.server_port}":
                    return self.send_error(403)
                prefix = "/" + owner.token + "/"
                name = unquote(urlsplit(self.path).path)
                if not name.startswith(prefix):
                    return self.send_error(404)
                relative_name = name[len(prefix):]
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
                    self.send_header("Content-Security-Policy", "default-src 'none'; "+images+" style-src 'unsafe-inline'; base-uri 'none'; form-action 'none';" + script + connect)
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

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/{self.token}/index.html"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
