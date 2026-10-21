from __future__ import annotations

import base64
import hashlib
import html
import re
import threading
import time
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .archive import Archive, item_key, safe_name, STATES
from .browser import BrowserUnavailable, launch_browser


APPROVAL_BASE = "/segio/works/approval/"
BOXES = {
    "my_complete.php": "완료함", "my_making_text.php": "작성함",
    "app_text_do_list.php": "결재 대기함", "app_text_comment.php": "의견함",
    "app_text_list.php": "진행함", "my_cancel_text.php": "반려함",
    "refer_list.php": "참조함", "receive_text.php": "수신함", "favorite.php": "즐겨찾기",
    "my_circulation_list.php": "회람함", "general_new_list.php": "일반문서 작성함",
    "general_send_list.php": "일반문서 발신함", "general_recv_list.php": "일반문서 수신함",
}
DETAILS = {"view_format.php", "view_format_for_link.php", "general_view.php", "text_sender_view.php"}
MUTATION = re.compile(r"delete|remove|restore|backup_document|logout|clean(?:spam|trash)|act_rm|approve|reject", re.I)
ALLOWED_QUERY = {"no", "uid", "view", "page", "send_no", "rotation_no", "sno", "link_no", "chk_link_view"}
DEFAULT_SERVER = "http://lssgroup.co.kr/icons/app/cms/?html=/home/login.html&shell=/index.shell:483"


class CollectionError(RuntimeError):
    pass


class Cancelled(CollectionError):
    pass


def origin(url: str) -> tuple:
    u = urlsplit(url)
    return u.scheme.lower(), (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80)


def clean_source(url: str) -> str:
    u = urlsplit(url)
    q = {k: v for k, v in parse_qs(u.query).items() if k in ALLOWED_QUERY}
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q, doseq=True), ""))


def canonical(url: str) -> str:
    u = urlsplit(url)
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(sorted(parse_qs(u.query).items()), doseq=True), ""))


def readonly_url(url: str) -> bool:
    u = urlsplit(url)
    if u.scheme not in {"http", "https"} or u.username or u.password:
        return False
    if re.search(r"(?:delete|remove|logout|action|edit|write|regist)\.php$", u.path, re.I):
        return False
    return not any(MUTATION.search(k + "=" + v) for k, vals in parse_qs(u.query).items() for v in vals)


@dataclass
class Config:
    server: str
    username: str
    output: Path
    approvals: bool = True
    mail: bool = True
    pdf: bool = True
    allow_http: bool = False
    headless: bool = False
    timeout_ms: int = 20000
    max_pages: int = 300
    max_items: int = 10000
    max_attachment_mb: int = 100
    manual_login: bool = False
    browser_executable: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    progress: object = field(default=None, repr=False, compare=False)

    def validate(self):
        self.server = html.unescape(self.server.strip().replace("\\&", "&"))
        u = urlsplit(self.server)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.fragment:
            raise CollectionError("서버 주소는 http(s)://호스트[:포트][/로그인경로] 형식이어야 합니다.")
        if any(re.search(r"pass|token|session|secret|auth|^sid$|^key$", key, re.I) for key in parse_qs(u.query)):
            raise CollectionError("로그인 주소에 암호·인증 토큰을 넣지 마세요.")
        if u.scheme == "http" and not self.allow_http:
            raise CollectionError("HTTP 서버입니다. 암호화되지 않은 HTTP 연결 허용을 선택해야 합니다.")
        if not self.username.strip():
            raise CollectionError("사용자 ID를 입력하세요.")
        if self.max_pages < 1 or self.max_items < 1:
            raise CollectionError("수집 제한은 1 이상이어야 합니다.")


SNAPSHOT = r'''selector => {
 const root=document.querySelector(selector)||document.body;
 const props=['display','color','background-color','font-family','font-size','font-weight','font-style','line-height','text-align','vertical-align','white-space','border','border-collapse','border-spacing','padding','margin','width','max-width','height','float','clear','text-decoration'];
 function clone(el){
   const c=el.cloneNode(false);
   if(el.nodeType!==Node.ELEMENT_NODE)return c;
   const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden')return document.createTextNode('');
   c.setAttribute('style',props.map(p=>p+':'+s.getPropertyValue(p)).join(';'));
   if(el.tagName==='IMG')c.setAttribute('src',el.currentSrc||el.src);
   if(el.tagName==='A')c.setAttribute('href',el.href);
   for(const n of el.childNodes)c.append(clone(n));return c;
 }
 return clone(root).outerHTML;
}'''


class Collector:
    def __init__(self, config: Config, log=lambda message: None):
        config.validate()
        self.config = config
        self.log_callback = log
        u = urlsplit(config.server)
        self.base = urlunsplit((u.scheme, u.netloc, "/", "", ""))
        self.archive: Archive | None = None
        self.context = None
        self.page = None
        self.browser = None
        self.authenticated = False
        self.seen: set[str] = set()
        self.asset_cache = {}
        self.scope = {origin(config.server)}
        self.login_actions = set()

    def log(self, message: str, level="info"):
        self.log_callback(message)
        if self.archive:
            self.archive.event(level, message)

    def checkpoint(self):
        if self.config.cancel.is_set():
            raise Cancelled("수집을 중단했습니다. 이미 저장한 자료는 보관됩니다.")

    def in_scope(self, url):
        return origin(url) in self.scope and readonly_url(url)

    def guard(self, route):
        req = route.request
        u = urlsplit(req.url)
        if u.scheme in {"data", "blob", "about"}:
            return route.continue_()
        # Same-host HTTPS upgrades are permitted; HTTPS downgrades and other hosts are not.
        if (u.scheme == "https" and u.hostname == urlsplit(self.base).hostname):
            self.scope.add(origin(req.url))
        if origin(req.url) not in self.scope:
            return route.abort()
        if self.authenticated and (req.method not in {"GET", "HEAD"} or not readonly_url(req.url)):
            return route.abort()
        if not self.authenticated and req.method not in {"GET", "HEAD", "POST"}:
            return route.abort()
        if not self.authenticated and req.method == "POST":
            if canonical(req.url) not in self.login_actions:
                # SEGIO can create a popup or change the form's HTTPS action
                # in its submit handler. Trust only password forms actually
                # present in this context, still within the configured host.
                self.register_login_forms()
            if canonical(req.url) not in self.login_actions:
                return route.abort()
            self._last_login_base = urljoin(req.url, '/')
        return route.continue_()

    @staticmethod
    def frame_transition(exc):
        from playwright.sync_api import Error
        return isinstance(exc, Error) and any(value in str(exc).lower() for value in (
            'frame was detached', 'execution context was destroyed',
            'cannot find context with specified id', 'most likely because of a navigation'))

    def login_pages(self):
        pages = list(self.context.pages) if self.context else [self.page]
        return [page for page in pages if not page.is_closed()]

    def register_login_forms(self):
        for page in self.login_pages():
            for frame in page.frames:
                try:
                    actions = frame.locator('input[type="password"]').evaluate_all(
                        'els=>els.map(e=>e.form?.action).filter(Boolean)')
                except Exception as exc:
                    if self.frame_transition(exc) or page.is_closed():
                        continue
                    raise
                for action in actions:
                    if urlsplit(action).scheme == 'https' and urlsplit(action).hostname == urlsplit(self.base).hostname:
                        self.scope.add(origin(action))
                    if self.in_scope(action):
                        self.login_actions.add(canonical(action))

    def login_present(self, page) -> bool:
        moving = False
        for frame in page.frames:
            try:
                if frame.locator('input[type="password"]').count():
                    return True
            except Exception as exc:
                if not self.frame_transition(exc):
                    raise
                moving = True
        return moving

    def manual_login_complete(self):
        return not self.login_present(self.page)

    def confirm_login(self):
        pass

    def wait_for_manual_login(self):
        self.log('열린 브라우저에서 로그인하세요. 최대 3분 동안 로그인 완료를 확인합니다.')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            self.checkpoint()
            pages = self.login_pages()
            if not pages:
                raise CollectionError('로그인 브라우저가 닫혔습니다. 창을 유지한 채 다시 실행하세요. [BROWSER_CLOSED]')
            if self.page.is_closed():
                self.page = pages[0]
            self.register_login_forms()
            if self.manual_login_complete():
                return
            try:
                self.page.wait_for_timeout(500)
            except Exception:
                if not self.page.is_closed():
                    raise
        raise CollectionError('서버에서 직접 로그인을 확인하지 못했습니다. 같은 서버 주소로 로그인했는지와 서버 연결 기능의 인증 전달을 확인하세요. [MANUAL_LOGIN_TIMEOUT]')

    def navigate(self, url, page=None):
        self.checkpoint()
        if not self.in_scope(url):
            raise CollectionError("허용 범위 밖이거나 변경 동작이 포함된 경로입니다.")
        page = page or self.page
        response = page.goto(url, wait_until="domcontentloaded", timeout=self.config.timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        if not response or response.status >= 400:
            raise CollectionError(f"서버 응답 오류: {response.status if response else '응답 없음'}")
        if self.authenticated and self.login_present(page):
            raise CollectionError("로그인 세션이 만료되었거나 문서 접근 권한이 없습니다.")
        text = page.locator("body").inner_text()
        if len(text) < 2000 and re.search(r"access denied|permission denied|접근.*거부|권한이 없|로그인이 필요", text, re.I):
            raise CollectionError("접근 권한이 없는 화면입니다.")
        return response

    def login(self, password: str, *, probe=True):
        self.navigate(self.config.server)
        self.register_login_forms()
        if not self.config.manual_login:
            login_frame = next((f for f in self.page.frames if f.locator('input[type="password"]').count()), None)
            if login_frame is None:
                raise CollectionError("로그인 폼을 찾지 못했습니다. 로그인 페이지 주소나 브라우저 직접 로그인을 사용하세요.")
            uid = login_frame.locator('input[name="uid" i], input[name="userid" i], input[name="username" i], input[name="id" i]')
            if not uid.count():
                raise CollectionError("사용자 ID 입력란을 찾지 못했습니다. 브라우저 직접 로그인을 사용하세요.")
            uid.first.fill(self.config.username)
            pw = login_frame.locator('input[type="password"]').first
            pw.fill(password)
            # Submit the form itself, retaining hidden fields, target iframe and server-generated action.
            action = pw.evaluate("e=>e.form.action")
            if not self.in_scope(action):
                raise CollectionError("로그인 목적지가 입력한 서버와 다릅니다. 해당 HTTPS 로그인 주소를 서버 주소로 입력하세요.")
            with self.page.expect_response(lambda r: r.request.method == "POST" and
                                           urlsplit(r.url).path == urlsplit(action).path,
                                           timeout=self.config.timeout_ms) as submitted:
                pw.evaluate("e=>{const f=e.form;const b=f.querySelector('input[type=submit],button[type=submit],button:not([type])');b?f.requestSubmit(b):f.requestSubmit()}")
            if submitted.value.status >= 400:
                raise CollectionError("로그인이 거부되었습니다. ID·암호를 확인하세요.")
            if urlsplit(action).scheme == "https":
                a = urlsplit(action)
                self.base = urlunsplit((a.scheme, a.netloc, "/", "", ""))
            try:
                self.page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
        else:
            self.wait_for_manual_login()
        # Probe an authenticated page. A successful POST or a session cookie alone is insufficient.
        if probe:
            probe_path = APPROVAL_BASE + "index.php" if self.config.approvals else "/_mail/inbox.mbox/"
            self.navigate(urljoin(self.base, probe_path))
            if self.login_present(self.page):
                raise CollectionError("로그인이 확인되지 않았습니다. ID·암호와 로그인 방식을 확인하세요.")
            markers = self.page.locator('a[href*="my_complete.php"], a[href*="view_format.php"], a[href*="/_mail/"], .appr_table, .mail_list')
            mail_info = self.page.evaluate("() => Array.isArray(window.Mail_info) && window.Mail_info.length>1")
            if not markers.count() and not mail_info:
                raise CollectionError("로그인 후 결재·메일 화면을 확인하지 못했습니다. 서버 화면에 맞춘 연결 조정이 필요합니다.")
        self.confirm_login()
        self.authenticated = True
        # Server-generated login may upgrade HTTP to HTTPS.
        u = urlsplit(getattr(self, '_verified_login_base', None) or self.page.url)
        self.base = urlunsplit((u.scheme, u.netloc, "/", "", ""))
        if u.scheme == "https":
            self.scope = {s for s in self.scope if s[0] == "https"}
        if self.config.manual_login and probe:
            self.log("직접 로그인 계정과 입력 ID의 일치 여부를 자동 검증하지 못했습니다. 보관 대상 계정을 확인하세요.", "warning")

    def run(self, password: str) -> Path:
        from playwright.sync_api import sync_playwright

        self.archive = Archive(self.config.output, clean_source(self.config.server), self.config.username)
        state = "partial"
        try:
            with sync_playwright() as p:
                self.browser = launch_browser(p.chromium, headless=self.config.headless,
                                              explicit=self.config.browser_executable, log=self.log)
                self.context = self.browser.new_context(accept_downloads=False, service_workers="block")
                self.context.set_default_timeout(self.config.timeout_ms)
                self.context.route("**/*", self.guard)
                self.page = self.context.new_page()
                self.log("서버 로그인 확인 중")
                self.login(password)
                password = ""
                self.log("로그인 확인. 문서·메일 수집을 시작합니다.")
                if self.config.approvals:
                    self.collect_approvals()
                if self.config.mail:
                    self.collect_mail()
                self.checkpoint()
                coverage = self.archive.manifest["coverage"]
                state = "collected_with_warnings" if self.archive.manifest["warnings"][1:] or any(
                    x.get("status") != "enumerated" for x in coverage) else "collected"
        except Cancelled as exc:
            state = "cancelled"
            self.log(str(exc), "warning")
        except Exception as exc:
            # Browser exceptions can contain request headers/URLs: never persist their full text.
            message = str(exc) if isinstance(exc, (CollectionError, BrowserUnavailable)) else f"수집 오류 ({type(exc).__name__}). 서버 접근·브라우저 설치를 확인하세요."
            self.log(message, "error")
            state = "failed" if not self.seen else "partial"
        finally:
            password = ""
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
            self.archive.finish(state)
        self.log_callback(f"{STATES.get(state, state)}. 저장 위치: {self.archive.path}")
        return self.archive.path

    def links(self, page=None):
        return (page or self.page).locator("a[href]").evaluate_all(
            "els=>els.map(e=>({url:e.href,text:e.innerText.trim(),inBody:!!e.closest('.mail_view_context')}))")

    def fetch(self, url, maximum=None):
        self.checkpoint()
        if not self.in_scope(url):
            raise CollectionError("다른 서버의 자료는 자동 다운로드하지 않습니다.")
        r = self.context.request.get(url, timeout=self.config.timeout_ms, max_redirects=0)
        try:
            if r.status >= 300:
                raise CollectionError(f"다운로드 응답 오류: {r.status}")
            limit = maximum or self.config.max_attachment_mb * 1024 * 1024
            declared = r.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise CollectionError("파일 크기가 수집 제한을 초과합니다.")
            data = r.body()
            if len(data) > limit:
                raise CollectionError("파일 크기가 수집 제한을 초과합니다.")
            return data, r.headers
        finally:
            r.dispose()

    def attachment(self, url, title, directory):
        data, headers = self.fetch(url)
        ctype = headers.get("content-type", "").lower()
        if "text/html" in ctype or data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise CollectionError("첨부 다운로드가 HTML 화면을 반환했습니다. 파일은 보관하지 않았습니다.")
        from email.message import Message
        message = Message()
        message["Content-Disposition"] = headers.get("content-disposition", "")
        filename = message.get_filename() or title or "attachment.bin"
        name = safe_name(filename, 100)
        digest = hashlib.sha256(data).hexdigest()[:16]
        rel = f"{directory}/attachments/{digest}_{name}"
        self.archive.write(rel, data)
        return {"name": filename, "path": rel, "bytes": len(data)}

    def freeze(self, page, directory, title, attachments, related=None):
        kind_mail = bool(page.locator(".mail_view_div").count())
        selector = ".mail_view_div" if kind_mail else next(
            (s for s in (".gwcore_zone", ".main_wrap", "body") if page.locator(s).count()), "body")
        raw = page.evaluate(SNAPSHOT, selector)
        soup = BeautifulSoup(raw, "html.parser")
        for el in list(soup.find_all(["script", "style", "link", "meta", "base", "iframe", "object", "embed", "svg", "video", "audio", "source", "button", "textarea", "select"])):
            el.decompose()
        for el in list(soup.find_all("input")):
            if el.get("type", "").lower() in {"text", "number", "date"}:
                el.replace_with(soup.new_string(el.get("value", "")))
            else:
                el.decompose()
        for el in soup.find_all("form"):
            el.unwrap()
        for el in soup.find_all(True):
            for attr in list(el.attrs):
                if attr not in {"style", "src", "href", "colspan", "rowspan", "alt", "title", "width", "height"}:
                    del el[attr]
            if "style" in el.attrs:
                el["style"] = re.sub(r"url\s*\([^)]*\)|expression\s*\([^)]*\)", "", el["style"], flags=re.I)
            if el.name == "img":
                src = urljoin(page.url, el.get("src", ""))
                try:
                    if src.startswith("data:image/") and len(src) < 12 * 1024 * 1024:
                        if not src.lower().startswith(("data:image/png;", "data:image/jpeg;", "data:image/gif;", "data:image/webp;")):
                            raise CollectionError("지원하지 않는 이미지 형식")
                    else:
                        if src not in self.asset_cache:
                            data, _ = self.fetch(src, 10 * 1024 * 1024)
                            mime = image_mime(data)
                            if not mime:
                                raise CollectionError("이미지 파일 응답이 아닙니다.")
                            self.asset_cache[src] = f"data:{mime};base64," + base64.b64encode(data).decode()
                        src = self.asset_cache[src]
                    el["src"] = src
                except Exception:
                    el.replace_with(soup.new_string("[이미지 미확보: " + el.get("alt", "") + "]"))
                    self.log(f"{title}: 일부 이미지 미확보", "warning")
            elif el.name == "a":
                el.attrs.pop("href", None)
            else:
                el.attrs.pop("src", None)
        text = soup.get_text(" ", strip=True)
        from html import escape
        links = "".join(f'<li><a href="{escape(relative_link(directory, a["path"]), quote=True)}">{escape(a["name"])}</a></li>' for a in attachments)
        linked = "".join(f'<li><a href="{escape(relative_link(directory, path), quote=True)}">관련 문서 {escape(label)}</a></li>' for label, path in (related or []))
        document = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{escape(title)}</title><style>body{{font-family:'Malgun Gothic',sans-serif;padding:20px;overflow-wrap:anywhere}}img{{max-width:100%}}table{{max-width:100%}}@media print{{.archive-info{{display:none}}}}</style></head><body>
<div class="archive-info"><h1>{escape(title)}</h1><p>오프라인 보관본 · 수집 당시 화면. 서버 계정 없이 열람할 수 있습니다.</p><ul>{links}{linked}</ul><hr></div>{soup}</body></html>'''
        path = self.archive.write(directory + "/document.html", document.encode())
        return path, text

    def make_pdf(self, html_path, directory):
        if not self.config.pdf:
            return ""
        # A separate context never carries login cookies and cannot access the network.
        offline = self.browser.new_context(service_workers="block")
        offline.route("**/*", lambda route: route.continue_()
                      if urlsplit(route.request.url).scheme in {"file", "data", "about"}
                      else route.abort())
        page = offline.new_page()
        try:
            # Self-contained HTML uses data images; no file:// permission or network is required.
            page.set_content((self.archive.path / html_path).read_text(encoding="utf-8"), wait_until="load")
            page.emulate_media(media="screen")
            width = page.evaluate("() => document.body.scrollWidth")
            data = page.pdf(format="A4", print_background=True, prefer_css_page_size=True,
                            scale=max(0.1, min(1, 720 / max(width, 1))))
            return self.archive.write(directory + "/document.pdf", data)
        except Exception:
            self.log(f"{directory}: PDF 생성 실패. HTML 보관본을 확인하세요.", "warning")
            return ""
        finally:
            offline.close()

    def enumerate_box(self, start, folder, kind):
        pending = [start]
        visited = set()
        identities = set()
        items = []
        expected = None
        truncated = False
        while pending:
            self.checkpoint()
            url = pending.pop(0)
            normalized = canonical(url)
            if normalized in visited:
                continue
            if len(visited) >= self.config.max_pages:
                truncated = True
                break
            visited.add(normalized)
            self.navigate(url)
            if expected is None:
                txt = self.page.locator("body").inner_text()
                match = re.search(r"(?:문서|documents?|전체|total)\s*[:：]?\s*([\d,]+)\s*(?:건|개)?", txt, re.I)
                if match:
                    expected = int(match.group(1).replace(",", ""))
            for link in self.links():
                target = link["url"]
                if not self.in_scope(target):
                    continue
                u = urlsplit(target)
                if kind == "approval":
                    is_item = Path(u.path).name in DETAILS and bool(parse_qs(u.query).get("no") or parse_qs(u.query).get("uid"))
                else:
                    # Take only rendered mail-row links, not navigation/menu/action links.
                    is_item = self.mail_detail_url(target, start)
                    if is_item:
                        row_links = self.page.locator('.mail_list a[href], #mail-list a[href], [data-mail-list] a[href]').evaluate_all("els=>els.map(a=>a.href)")
                        is_item = target in row_links
                if is_item:
                    identity = self.document_identity(target) if kind == "approval" else canonical(target)
                    if identity not in identities:
                        identities.add(identity)
                        items.append((target, link["text"] or identity))
                if self.pagination_url(target, start) and canonical(target) not in visited:
                    if target not in pending:
                        pending.append(target)
            # SEGIO renders page navigation as anchors. Unrecognized forms are reported, not guessed POSTs.
        coverage = {"kind": kind, "folder": folder, "pages": len(visited),
                    "discovered": len(items), "expected": expected, "saved": 0,
                    "status": "limited" if truncated else "enumerated"}
        if expected is not None and expected != len(items):
            coverage["status"] = "count_mismatch"
        if kind == "mail" and expected is None:
            coverage["status"] = "count_unverified"
        if not items and expected is None:
            coverage["status"] = "empty_unverified"
        self.archive.manifest["coverage"].append(coverage)
        self.archive.save_manifest()
        return items, coverage

    def pagination_url(self, target, start):
        u, s = urlsplit(target), urlsplit(start)
        if u.path == s.path:
            q, sq = parse_qs(u.query), parse_qs(s.query)
            # Only a page-number difference; filters and destructive actions are never followed.
            if q.get("page") and q["page"][0].isdigit():
                def filters(values):
                    return {k:v for k,v in values.items()
                            if k not in {"page", "shell"} and v != [""] and not (k == "find" and v == ["all"])}
                return filters(q) == filters(sq)
        if "/_mail/" in s.path:
            # SEGIO also uses inbox.mbox,02/ style pagination.
            return bool(re.fullmatch(re.escape(s.path.rstrip("/")) + r",\d+/?", u.path))
        return False

    def mail_detail_url(self, target, start):
        u, s = urlsplit(target), urlsplit(start)
        if not u.path.startswith("/_mail/") or self.pagination_url(target, start):
            return False
        if u.path.rstrip("/") == s.path.rstrip("/"):
            return any(k in parse_qs(u.query) for k in ("no", "id", "mail", "msg", "mid"))
        return u.path.startswith(s.path.rstrip("/") + "/") and u.path.rstrip("/") != s.path.rstrip("/")

    def document_identity(self, url):
        u = urlsplit(url)
        q = parse_qs(u.query)
        kind = "general" if Path(u.path).name == "general_view.php" else "approval"
        return kind + ":" + (q.get("no") or q.get("uid"))[0]

    def collect_approvals(self):
        self.navigate(urljoin(self.base, APPROVAL_BASE + "index.php"))
        boxes = {}
        for link in self.links():
            name = Path(urlsplit(link["url"]).path).name
            if name in BOXES and self.in_scope(link["url"]):
                boxes[link["url"]] = BOXES[name]
        if not boxes:
            self.log("결재함 메뉴를 찾지 못했습니다. 빈 백업으로 간주하지 않습니다.", "error")
            self.archive.manifest["coverage"].append({"kind": "approval", "status": "not_found"})
            return
        for start, folder in boxes.items():
            self.log(f"결재 {folder}: 목록 확인")
            try:
                items, coverage = self.enumerate_box(start, folder, "approval")
                for url, title in items:
                    self.checkpoint()
                    if self.save_approval(url, title, folder):
                        coverage["saved"] += 1
                if coverage["saved"] != coverage["discovered"]:
                    coverage["status"] = "item_failure"
            except Cancelled:
                raise
            except Exception:
                self.log(f"결재 {folder}: 목록 수집 실패. 세션·권한·화면 구조를 확인하세요.", "error")
                self.archive.manifest["coverage"].append({"kind": "approval", "folder": folder, "status": "failed"})

    def save_approval(self, url, title, folder, depth=0):
        key = item_key("approval", self.document_identity(url))
        if self.archive.add_folder(key, folder):
            return True
        if key in self.seen:
            return False
        if len(self.seen) >= self.config.max_items or depth > 5:
            self.log("문서 수 또는 연계 문서 깊이 제한에 도달했습니다. 일부 문서를 수집하지 못했습니다.", "warning")
            return False
        self.seen.add(key)
        page = self.context.new_page()
        directory = "documents/" + key
        try:
            self.navigate(url, page)
            heading = page.locator('.appr_vtitle_wrap h2, .works_title h2').first
            if heading.count():
                title = heading.inner_text().strip() or title
            links = self.links(page)
            attachments = []
            for link in links:
                if Path(urlsplit(link["url"]).path).name in {"download.php", "attach_down.php"} and self.in_scope(link["url"]):
                    try:
                        attachments.append(self.attachment(link["url"], link["text"], directory))
                    except Exception:
                        self.log(f"{title}: 첨부파일 다운로드 실패", "warning")
            related = []
            for link in links:
                if Path(urlsplit(link["url"]).path).name == "view_format_for_link.php" and self.in_scope(link["url"]) and parse_qs(urlsplit(link["url"]).query).get("no"):
                    other = item_key("approval", self.document_identity(link["url"]))
                    if other != key and self.save_approval(link["url"], link["text"], "연계 문서", depth+1):
                        related.append((link["text"], "documents/" + other + "/document.html"))
            # Source print template needs AJAX-loaded original documents. Wait for their content first.
            if page.locator("#orig_doc_area").count():
                try:
                    page.wait_for_function("() => !document.querySelector('#orig_doc_no')?.value || !!document.querySelector('#orig_doc_area')?.innerText.trim()", timeout=5000)
                except Exception:
                    self.log(f"{title}: 연계 원본 표시를 확인하지 못했습니다.", "warning")
            html_path, body = self.freeze(page, directory, title, attachments, related)
            pdf = self.make_pdf(html_path, directory)
            self.archive.add(key=key, kind="approval", title=title, folder=folder,
                             source=clean_source(url), body=body, html_path=html_path,
                             pdf=pdf, attachments=attachments)
            self.log(f"결재 저장: {title}")
            return True
        except Cancelled:
            raise
        except Exception:
            self.log(f"결재 문서 수집 실패: {title}", "error")
            return False
        finally:
            page.close()

    def collect_mail(self):
        self.log("메일함 확인. 서버에 따라 메일 열람 시 읽음 상태가 변경될 수 있습니다.", "warning")
        try:
            self.navigate(urljoin(self.base, "/_mail/inbox.mbox/?shell=/segio/msg/mail.shell"))
            info = self.page.evaluate("() => typeof Mail_info !== 'undefined' ? Mail_info : null")
            boxes = {}
            if isinstance(info, list):
                for group in info[1:]:
                    if not isinstance(group, list):
                        continue
                    for box in group:
                        if isinstance(box, list) and len(box) >= 3 and isinstance(box[2], str):
                            url = urljoin(self.page.url, box[2])
                            if self.in_scope(url) and "/_mail/" in urlsplit(url).path:
                                count = box[3][1] if len(box)>3 and isinstance(box[3], list) and len(box[3])>1 else None
                                boxes[url] = (str(box[1]), int(count) if str(count).isdigit() else None)
            for link in self.links():
                u = urlsplit(link["url"])
                if re.fullmatch(r"/_mail/[^/]+\.mbox/?", u.path) and self.in_scope(link["url"]):
                    # Do not overwrite the same folder's exact count with a menu URL alias.
                    if not any(urlsplit(k).path.rstrip('/') == u.path.rstrip('/') for k in boxes):
                        boxes[link["url"]] = (link["text"] or Path(u.path).name, None)
            if not boxes:
                raise CollectionError("메일함 목록을 확인하지 못했습니다.")
            for start, (folder, count) in boxes.items():
                self.log(f"메일 {folder}: 목록 확인")
                try:
                    items, coverage = self.enumerate_box(start, folder, "mail")
                    if count is not None:
                        coverage["expected"] = count
                        coverage["status"] = "enumerated" if count == len(items) else "count_mismatch"
                    for url, title in items:
                        if self.save_mail(url, title, folder):
                            coverage["saved"] += 1
                    if coverage["saved"] != len(items):
                        coverage["status"] = "item_failure"
                except Cancelled:
                    raise
                except Exception:
                    self.log(f"메일 {folder}: 목록 수집 실패", "error")
                    self.archive.manifest["coverage"].append({"kind": "mail", "folder": folder, "status": "failed"})
        except Cancelled:
            raise
        except Exception:
            self.log("메일함 구조를 확인하지 못했습니다. 운영 서버에 맞춘 연결 조정이 필요합니다.", "error")
            self.archive.manifest["coverage"].append({"kind": "mail", "status": "not_found"})

    def save_mail(self, url, title, folder):
        key = item_key("mail", canonical(url))
        if self.archive.add_folder(key, folder):
            return True
        if len(self.seen) >= self.config.max_items:
            self.log("메일 수집 제한에 도달했습니다.", "warning")
            return False
        self.seen.add(key)
        directory = "documents/" + key
        page = self.context.new_page()
        try:
            self.navigate(url, page)
            if not page.locator('.mail_view_div, .mail_view_context, [data-mail-body]').count():
                raise CollectionError("메일 본문 표시 영역을 찾지 못했습니다.")
            # CC/BCC headers are collapsed in the supplied SEGIO template; preserve them too.
            if page.locator("#more_rec").count():
                page.locator("#more_rec").evaluate("e=>e.style.display='table-row-group'")
            heading = page.locator('.mail_view_top h3, [data-mail-subject]').first
            if heading.count():
                title = heading.inner_text().strip() or title
            eml = ""
            attachments = []
            for link in self.links(page):
                target = link["url"]
                if not self.in_scope(target):
                    continue
                if not link["inBody"] and (urlsplit(target).path.lower().endswith(".eml") or re.search(r"원문|original|raw message", link["text"], re.I)):
                    try:
                        data, _ = self.fetch(target)
                        parsed = BytesParser(policy=policy.default).parsebytes(data)
                        if not parsed.get("From") or not (parsed.get("Subject") or parsed.get("Date")) or b"\n\n" not in data.replace(b"\r\n", b"\n"):
                            raise CollectionError("유효한 메일 원문으로 확인되지 않았습니다.")
                        eml = self.archive.write(directory + "/message.eml", data)
                        for part in parsed.walk():
                            if not part.is_multipart() and part.get_filename():
                                body = part.get_payload(decode=True)
                                if body is not None:
                                    filename = part.get_filename()
                                    rel = directory + "/attachments/" + hashlib.sha256(body).hexdigest()[:16] + "_" + safe_name(filename, 100)
                                    self.archive.write(rel, body)
                                    attachments.append({"name": filename, "path": rel, "bytes": len(body)})
                    except Exception:
                        self.log(f"{title}: 메일 원문 다운로드 실패", "warning")
            candidates = page.locator('.mail_view_att a[href], [data-mail-attachments] a[href]').evaluate_all("els=>els.map(a=>({url:a.href,text:a.innerText}))")
            for link in candidates:
                if self.in_scope(link["url"]) and not urlsplit(link["url"]).path.endswith('.eml'):
                    try:
                        a = self.attachment(link["url"], link["text"], directory)
                        if not any(x["path"] == a["path"] for x in attachments):
                            attachments.append(a)
                    except Exception:
                        self.log(f"{title}: 메일 첨부파일 다운로드 실패", "warning")
            if not eml:
                self.log(f"{title}: 메일 원문 미확보. 본문 화면·확보된 첨부만 보관합니다.", "warning")
            html_path, body = self.freeze(page, directory, title, attachments)
            self.archive.add(key=key, kind="mail", title=title, folder=folder,
                             source=clean_source(url), body=body, html_path=html_path,
                             eml=eml, attachments=attachments,
                             status="captured" if eml else "rendered_only")
            self.log(f"메일 저장: {title}")
            return True
        except Cancelled:
            raise
        except Exception:
            self.log(f"메일 수집 실패: {title}", "error")
            return False
        finally:
            page.close()


def image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def relative_link(directory, target):
    import posixpath
    return posixpath.relpath(target, directory)
