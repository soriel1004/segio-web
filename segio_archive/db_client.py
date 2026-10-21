"""Employee SEGIO login, live personal DB/MBOX transfer and verified local archive.

The server bridge chooses the archive from native SEGIO identity. This client
never submits a target ID, DB credentials or commands to that bridge.
"""
from __future__ import annotations

import hashlib
import errno
import http.cookiejar
import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urljoin

from .browser import BrowserUnavailable, launch_browser
from .archive import safe_name
from .web import Collector, CollectionError, Config, origin
from .client_diagnostics import BridgeResponseError, response_summary, write_failure
from .progress import ProgressReporter
from .file_ops import file_diagnostic

BRIDGE = "/segio/archive/bridge.php"
PROTOCOL = "segio-login-archive/1"
LIVE_BRIDGE = "/segio/archive/live.php"
LIVE_PROTOCOL = "segio-live-archive/1"


def processing_error(exc, phase):
    """Only fixed codes escape this boundary; browser errors can contain secrets."""
    from playwright.sync_api import Error, TimeoutError as BrowserTimeout
    if isinstance(exc, BrowserTimeout):
        code = 'TIMEOUT'
        hint = '서버 응답 시간이 초과됐습니다. 서버 주소와 연결 상태를 확인하세요.'
    elif isinstance(exc, Error):
        known = re.search(r'\b(?:ERR_[A-Z_]+|ECONNREFUSED|ECONNRESET|ENOTFOUND|CERT_HAS_EXPIRED)\b', str(exc))
        allowed = {'ERR_CONNECTION_REFUSED','ERR_CONNECTION_RESET','ERR_CONNECTION_CLOSED',
                   'ERR_NAME_NOT_RESOLVED','ERR_CERT_AUTHORITY_INVALID','ERR_CERT_DATE_INVALID',
                   'ERR_SSL_PROTOCOL_ERROR','ERR_SSL_VERSION_OR_CIPHER_MISMATCH','ERR_FAILED',
                   'ECONNREFUSED','ECONNRESET','ENOTFOUND','CERT_HAS_EXPIRED'}
        code = known.group() if known and known.group() in allowed else (
            'LOGIN_FRAME_TRANSITION' if Collector.frame_transition(exc) else 'BROWSER_CONNECTION_ERROR')
        hint = '브라우저와 서버의 연결을 확인하세요. 로그인 창을 닫지 말고 다시 실행하세요.'
    elif isinstance(exc, json.JSONDecodeError):
        code, hint = 'INVALID_JSON', '서버 또는 보관 자료의 JSON 형식을 확인하지 못했습니다.'
    elif isinstance(exc, PermissionError):
        code, hint = 'FILE_ACCESS_DENIED', '저장 폴더 권한과 다른 프로그램에서 보관 파일을 열고 있는지 확인하세요.'
    elif isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            code, hint = 'DISK_FULL', '저장 공간이 부족합니다. 여유 공간을 확보하거나 다른 드라이브를 선택하세요.'
        elif exc.errno == errno.EFBIG:
            code, hint = 'FILE_TOO_LARGE', '드라이브의 단일 파일 크기 제한에 걸렸습니다. NTFS 또는 exFAT 드라이브를 선택하세요.'
        else:
            code, hint = 'FILE_IO_ERROR', '저장 공간과 폴더 접근 권한을 확인하세요.'
    else:
        code, hint = 'UNEXPECTED_PROCESSING_ERROR', '서버 연결과 설치 상태를 확인하세요.'
    error = CollectionError('%s 단계에서 실패했습니다. %s [%s]' % (phase, hint, code))
    error.filesystem_diagnostic = file_diagnostic(exc)
    return error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_opener(cookies):
    jar = http.cookiejar.CookieJar()
    for item in cookies:
        domain = item['domain']
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=item['name'], value=item['value'], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith('.'),
            domain_initial_dot=domain.startswith('.'), path=item['path'], path_specified=True,
            secure=item['secure'], expires=int(item['expires']) if item['expires']>0 else None,
            discard=item['expires']<=0, comment=None, comment_url=None,
            rest={'HttpOnly': item.get('httpOnly', False)}, rfc2109=False))
    # Default HTTPS certificate validation is deliberately retained.
    return urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))


def safe_member(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise CollectionError("ZIP에 안전하지 않은 파일 경로가 있습니다.")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix()!=name:
        raise CollectionError("ZIP에 안전하지 않은 파일 경로가 있습니다.")
    for part in path.parts:
        stem = part.split('.')[0].upper()
        if (part in {'.', '..'} or part.endswith((' ', '.')) or
                re.search(r'[<>:"|?*\x00-\x1f]', part) or
                stem in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(COM|LPT)[1-9]', stem)):
            raise CollectionError("ZIP에 Windows에서 사용할 수 없는 파일 경로가 있습니다.")
    return path


def unpack_verified(source: Path, destination: Path, username: str, checkpoint=lambda: None, progress=None):
    """Extract only manifest-listed regular files into a new private staging dir."""
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members)>200000:
            raise CollectionError("보관 ZIP의 파일 수가 제한을 초과합니다.")
        names = set()
        by_name = {}
        for member in members:
            path = safe_member(member.filename)
            folded = str(path).casefold()
            mode = member.external_attr >> 16
            if folded in names or member.is_dir() or (mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                raise CollectionError("ZIP에 중복 경로나 지원하지 않는 파일 형식이 있습니다.")
            names.add(folded)
            by_name[member.filename] = member
        if 'manifest.json' not in by_name or by_name['manifest.json'].file_size>16*1024*1024:
            raise CollectionError("정상적인 DB 보관함 manifest.json이 없습니다.")
        manifest_bytes = archive.read('manifest.json')
        manifest = json.loads(manifest_bytes)
        if (manifest.get('format')!='segio-db-html/1' or manifest.get('user')!=username or
                manifest.get('account_deletion_verified') is not False or
                manifest.get('state') not in {'collected', 'collected_with_warnings'}):
            raise CollectionError("보관본의 사용자 또는 보관 형식이 일치하지 않습니다.")
        files = manifest.get('files')
        if not isinstance(files, dict) or set(by_name)!=set(files)|{'manifest.json'} or 'index.html' not in files:
            raise CollectionError("ZIP 파일 목록과 검증 목록이 일치하지 않습니다.")
        total = sum(m.file_size for m in members)
        if total + 64*1024*1024 > shutil.disk_usage(destination).free:
            raise CollectionError("ZIP 압축을 풀 저장 공간이 부족합니다.")
        verified = 0
        report = progress or (lambda *args, **kwargs: None)
        report('verify', 0, total)
        for name, expected in files.items():
            checkpoint()
            path = safe_member(name)
            if (not isinstance(expected, dict) or type(expected.get('bytes')) is not int or
                    by_name[name].file_size!=expected['bytes'] or expected['bytes']<0 or
                    not re.fullmatch(r'[a-f0-9]{64}', str(expected.get('sha256', '')))):
                raise CollectionError("파일 검증 정보가 올바르지 않습니다.")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest = hashlib.sha256()
            count = 0
            with archive.open(by_name[name]) as incoming, target.open('xb') as outgoing:
                while block := incoming.read(1024*1024):
                    checkpoint()
                    count += len(block)
                    outgoing.write(block)
                    digest.update(block)
                    verified += len(block)
                    report('verify', verified, total)
            if count!=expected['bytes'] or digest.hexdigest()!=expected['sha256']:
                raise CollectionError("내려받은 보관 파일의 SHA-256 검증에 실패했습니다.")
        (destination / 'manifest.json').write_bytes(manifest_bytes)
        report('verify', total, total)
    return manifest


def publish_local(staging, archive, output, username):
    """Reserve a short name without replacing either an existing ZIP or folder."""
    for number in range(1, 100000):
        name = 'segio-' + safe_name(username, 100) + (f'-{number}' if number>1 else '')
        folder, zipped = output/name, output/(name+'.zip')
        if folder.exists():
            continue
        if archive is not None:
            try:
                with zipped.open('xb'):
                    pass
            except FileExistsError:
                continue
        elif zipped.exists():
            continue
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            if archive is not None:
                zipped.unlink()
            continue
        try:
            # Windows cannot replace an existing directory using MoveFileEx.
            # Move verified children into our exclusively reserved directory.
            for child in staging.iterdir():
                os.replace(child, folder/child.name)
            staging.rmdir()
            if archive is not None:
                os.replace(archive, zipped)
            return folder
        except BaseException:
            shutil.rmtree(folder)
            if archive is not None:
                zipped.unlink(missing_ok=True)
            raise
    raise CollectionError("같은 이름의 보관함이 너무 많습니다. 다른 저장 폴더를 선택하세요.")


def reserve_personal_folder(output, username):
    """Index in the final reserved folder, never move open verified originals."""
    for number in range(1, 100000):
        name = 'segio-' + safe_name(username, 100) + (f'-{number}' if number > 1 else '')
        folder = output/name
        if (output/(name + '.zip')).exists():
            continue
        try:
            folder.mkdir(mode=0o700)
            return folder
        except FileExistsError:
            continue
    raise CollectionError('같은 이름의 보관함이 너무 많습니다. 다른 저장 폴더를 선택하세요.')


class PreparedDBCollector(Collector):
    keep_zip = True

    def __init__(self, config: Config, log=lambda _: None):
        super().__init__(config, log)
        self.phase = '시작'
        self.failure_path = None
        self.progress = ProgressReporter(config.progress)
        self.user_agent = None
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', config.username) or config.username.lower()=='guest':
            raise CollectionError("정확한 그룹웨어 ID를 입력하세요.")

    def info(self):
        response = self.context.request.get(urljoin(self.base, BRIDGE+'?action=info'),
                                            timeout=self.config.timeout_ms, max_redirects=0)
        try:
            if response.status==404:
                raise CollectionError("서버 연결 기능이 설치되지 않았습니다. 관리자가 prepare_login_archive.py를 먼저 실행해야 합니다.")
            if response.status==401:
                raise CollectionError("서버에서 SEGIO 로그인 사용자를 확인하지 못했습니다. 관리자에게 native_login_required 확인을 요청하세요.")
            if response.status!=200 or 'application/json' not in response.headers.get('content-type', ''):
                raise CollectionError("서버의 로그인 보관함 연결 응답을 확인하지 못했습니다. 서버 설치와 로그인 상태를 확인하세요.")
            data = response.body()
            if len(data)>65536:
                raise CollectionError("서버의 연결 응답이 올바르지 않습니다.")
            result = json.loads(data)
        finally:
            response.dispose()
        if not isinstance(result, dict) or result.get('protocol')!=PROTOCOL:
            raise CollectionError("서버 연결 기능의 버전이 일치하지 않습니다.")
        if result.get('username')!=self.config.username:
            raise CollectionError("실제 로그인 계정이 입력한 ID와 다릅니다. 해당 직원 계정으로 다시 로그인하세요.")
        record = result.get('archive')
        if record is None:
            raise CollectionError("이 직원의 DB 보관본이 아직 준비되지 않았습니다. 관리자가 해당 ID로 prepare_login_archive.py를 실행해야 합니다.")
        if (not isinstance(record, dict) or type(record.get('bytes')) is not int or record['bytes']<=0 or
                not re.fullmatch(r'[a-f0-9]{32}', str(record.get('version', ''))) or
                not re.fullmatch(r'[a-f0-9]{64}', str(record.get('sha256', '')))):
            raise CollectionError("서버의 보관본 검증 정보가 올바르지 않습니다.")
        return record

    def download_endpoint(self, record):
        return urljoin(self.base, BRIDGE+'?'+urlencode({'action':'download', 'version':record['version']}))

    def finalize_download(self, root, archive, destination):
        pass

    def validate_download(self, destination):
        pass

    def cleanup_remote(self):
        pass

    def after_verified_download(self, destination):
        pass

    def finish_remote_cleanup(self, destination):
        pass

    def record_failure(self, exception):
        try:
            self.failure_path = write_failure(self.config.output,exception,self.phase)
            self.log('오류 기록: '+str(self.failure_path))
        except OSError:
            self.log('오류 기록을 저장하지 못했습니다. 저장 폴더 권한과 공간을 확인하세요.')

    def receive(self, record):
        self.phase = '다운로드 저장 폴더 준비'
        output = self.config.output.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        if record['bytes'] + 64*1024*1024 > shutil.disk_usage(output).free:
            raise CollectionError("ZIP을 저장할 공간이 부족합니다.")
        with tempfile.TemporaryDirectory(prefix='.segio-download-', dir=output, ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            archive = root/'download.zip'
            destination = root/'contents'
            destination.mkdir(mode=0o700)
            endpoint = self.download_endpoint(record)
            opener = download_opener(self.context.cookies([endpoint]))
            user_agent = self.user_agent or self.page.evaluate('navigator.userAgent')
            request = urllib.request.Request(endpoint, headers={'Accept':'application/zip', 'User-Agent':user_agent})
            digest = hashlib.sha256()
            count = 0
            self.log("DB 보관 ZIP 다운로드 중")
            next_progress = time.monotonic()+5
            self.phase = '보관 ZIP 다운로드'
            self.progress('download', 0, record['bytes'])
            try:
                with opener.open(request, timeout=60) as incoming, archive.open('xb') as outgoing:
                    if incoming.status!=200 or incoming.headers.get_content_type()!='application/zip':
                        raise CollectionError("ZIP 대신 다른 서버 응답을 받았습니다. 로그인 상태를 확인하세요.")
                    if incoming.headers.get('Content-Length')!=str(record['bytes']):
                        raise CollectionError("서버 보관본 크기가 변경되었습니다. 다시 받아주세요.")
                    while block := incoming.read(1024*1024):
                        self.checkpoint()
                        count += len(block)
                        if count>record['bytes']:
                            raise CollectionError("보관 ZIP 크기가 검증 정보와 다릅니다.")
                        outgoing.write(block)
                        digest.update(block)
                        self.progress('download', count, record['bytes'])
                        if time.monotonic() >= next_progress:
                            self.log('다운로드 %.1f / %.1f GiB (%.0f%%)' %
                                     (count/1024**3, record['bytes']/1024**3, count*100/record['bytes']))
                            next_progress = time.monotonic()+5
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                raise CollectionError("다운로드가 끊겼거나 세션이 만료되었습니다. 다시 로그인해 받아주세요.") from None
            self.checkpoint()
            if count!=record['bytes'] or digest.hexdigest()!=record['sha256']:
                raise CollectionError("ZIP의 크기 또는 SHA-256 검증에 실패했습니다. 보관 완료로 처리하지 않았습니다.")
            self.log("ZIP과 내부 파일 검증 중")
            self.phase = '내려받은 파일 검증'
            if self.keep_zip:
                unpack_verified(archive, destination, self.config.username, self.checkpoint, self.progress)
                self.validate_download(destination)
            else:
                # Verify directly into a reserved persistent folder. Once
                # verified, temporary cleanup must never delete these files.
                destination = reserve_personal_folder(output, self.config.username)
                try:
                    unpack_verified(archive, destination, self.config.username, self.checkpoint, self.progress)
                    self.validate_download(destination)
                except BaseException:
                    try:
                        shutil.rmtree(destination)
                    except OSError:
                        self.log('검증 미완료 폴더를 자동 정리하지 못했습니다. 이 폴더는 백업 완료 상태가 아닙니다.')
                    raise
            try:
                if not self.keep_zip:
                    from .archive import atomic_write
                    manifest = json.loads((destination/'manifest.json').read_text(encoding='utf-8'))
                    manifest['local_processing'] = {'state':'pending'}
                    atomic_write(destination/'manifest.json', json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
                    archive.unlink()
                    self.after_verified_download(destination)
                self.checkpoint()
                self.finalize_download(root, archive, destination)
            except Exception as exc:
                if self.keep_zip:
                    raise
                reason = exc if isinstance(exc,CollectionError) else processing_error(exc,self.phase)
                error = CollectionError(str(reason)+'\n검증된 원본은 보존했습니다. ‘기존 백업 갱신’에서 다음 폴더를 선택하세요: '+str(destination)+' [LOCAL_INDEX_PENDING]')
                error.filesystem_diagnostic = getattr(reason,'filesystem_diagnostic',None)
                raise error from None
            self.progress('finalize')
            if not self.keep_zip:
                return destination
            return publish_local(destination, archive if self.keep_zip else None, output, self.config.username)

    def run(self, password: str) -> Path:
        from playwright.sync_api import sync_playwright
        try:
            self.checkpoint()
            with sync_playwright() as p:
                self.phase = '브라우저 시작'
                self.progress('login')
                self.browser = launch_browser(p.chromium, headless=self.config.headless,
                                              explicit=self.config.browser_executable, log=self.log)
                try:
                    self.context = self.browser.new_context(accept_downloads=False, service_workers='block')
                    self.context.set_default_timeout(self.config.timeout_ms)
                    self.context.route('**/*', self.guard)
                    self.page = self.context.new_page()
                    self.user_agent = self.page.evaluate('navigator.userAgent')
                    self.phase = 'SEGIO 로그인'
                    self.log("직원 SEGIO ID·암호로 로그인 확인 중")
                    # The bridge's native identity response is the authentication
                    # proof for this mode; approval-menu HTML is not required.
                    self.login(password, probe=False)
                    password = ''
                    self.checkpoint()
                    self.phase = '서버 연결 기능 확인'
                    record = self.info()
                    try:
                        prepared = datetime.fromisoformat(record['prepared_at']).astimezone(
                            timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S KST')
                    except (ValueError, KeyError, TypeError):
                        prepared = '확인 필요'
                    self.log("본인 자료 확인 · 추출 기준 시각: " + prepared)
                    result = self.receive(record)
                    self.finish_remote_cleanup(result)
                    self.log("다운로드와 파일 검증 완료: " + str(result))
                    return result
                finally:
                    try:
                        self.cleanup_remote()
                    except Exception:
                        self.log('서버 임시 ZIP 삭제를 확인하지 못했습니다. 저장된 백업은 유지됩니다. 서버 정리 상태를 확인하세요.')
                    try:
                        self.browser.close()
                    except Exception:
                        pass
                    self.context = self.page = self.browser = None
        except (CollectionError, BrowserUnavailable) as exc:
            self.record_failure(exc)
            raise
        except Exception as exc:
            # Playwright/urllib errors can contain session URLs and cookies.
            error = processing_error(exc,self.phase)
            self.record_failure(error)
            raise error from None
        finally:
            password = ''


class DBCollector(PreparedDBCollector):
    """Fresh employee-scoped DB query and original server mailbox transfer."""
    keep_zip = False

    def __init__(self, config, log=lambda _: None):
        super().__init__(config, log)
        self.csrf = None
        self.job = None
        self.cleanup_confirmed = False
        self.cleanup_cancel_requested = False
        self.cleanup_attempts = 0
        self.cleanup_error_code = None

    def live_call(self, action, *, post=False, pending_login=False, timeout_ms=None, **fields):
        if action != 'cleanup':
            self.phase = {'info':'서버 로그인 확인','start':'서버 추출 요청','status':'서버 추출 상태 확인'}.get(action,'서버 연결')
        query = {'action':action}
        if action in {'status','download','cleanup'}:
            query['job'] = self.job
        url = urljoin(self.base, LIVE_BRIDGE+'?'+urlencode(query))
        options = {'timeout':timeout_ms or self.config.timeout_ms, 'max_redirects':0}
        if self.user_agent:
            options['headers'] = {'User-Agent':self.user_agent}
        if post:
            response = self.context.request.post(url, form={'csrf':self.csrf, **fields},
                                                 **options)
        else:
            response = self.context.request.get(url, **options)
        try:
            if pending_login and response.status==401:
                return None
            if response.status==404 and action=='info':
                raise CollectionError('실시간 DB·MBOX 연결 기능이 없습니다. 서버에서 install_live_archive.py를 한 번 설치해야 합니다. [BRIDGE_NOT_INSTALLED]')
            if pending_login and response.status in {301,302,303,307,308}:
                location = urljoin(url,response.headers.get('location',''))
                if origin(location) in self.scope:
                    return None
                raise CollectionError('로그인 확인 주소가 다른 서버로 이동합니다. 실제 SEGIO 서버 주소를 확인하세요. [LOGIN_ORIGIN_MISMATCH]')
            body = response.body()
            diagnostic = response_summary(action,response.status,response.headers.get('content-type',''),body)
            if len(body)>65536:
                raise BridgeResponseError('서버 추출 응답이 너무 큽니다. 연결 주소와 PHP 실행 상태를 확인하세요.',
                                          'BRIDGE_RESPONSE_TOO_LARGE',diagnostic)
            if diagnostic['content_type']!='application/json':
                if pending_login and response.status==200 and re.search(rb'<input\b[^>]*\btype\s*=\s*[\'"]?password\b',body,re.I):
                    return None
                if diagnostic['body_kind']=='php_source':
                    raise BridgeResponseError('서버가 PHP 실행 결과 대신 PHP 소스를 반환했습니다. 서버의 PHP 실행 설정을 확인하세요.',
                                              'BRIDGE_PHP_SOURCE',diagnostic)
                if diagnostic['body_kind']=='login_form':
                    raise BridgeResponseError('서버가 로그인 화면을 반환했습니다. 접속 주소와 그룹웨어 로그인 상태를 확인하고 다시 로그인하세요.',
                                              'BRIDGE_LOGIN_REQUIRED',diagnostic)
                raise BridgeResponseError('서버 연결 주소가 JSON 대신 HTML 또는 다른 응답을 반환했습니다. 서버의 PHP 실행·인증 연결을 확인하세요.',
                                          'BRIDGE_NOT_JSON',diagnostic)
            try:
                value = json.loads(body)
            except (ValueError,UnicodeError):
                raise BridgeResponseError('서버 연결 기능이 올바른 JSON을 반환하지 않았습니다. PHP 오류와 설정 파일 접근 상태를 확인하세요.',
                                          'BRIDGE_INVALID_JSON',diagnostic) from None
            if isinstance(value,dict) and value.get('protocol')==LIVE_PROTOCOL and value.get('error'):
                if pending_login and value['error']=='native_login_required':
                    return None
                raise CollectionError(self.error_message(value['error']))
            if response.status>=300:
                code = value.get('error','') if isinstance(value,dict) else ''
                raise CollectionError(self.error_message(code))
        finally:
            response.dispose()
        if not isinstance(value,dict) or value.get('protocol')!=LIVE_PROTOCOL:
            raise CollectionError('서버 실시간 추출 기능의 버전이 일치하지 않습니다.')
        if value.get('username')!=self.config.username:
            raise CollectionError('실제 로그인 계정이 입력한 ID와 다릅니다. 해당 직원 계정으로 다시 로그인하세요.')
        return value

    @staticmethod
    def error_message(code):
        return {
            'service_not_configured':'직원 로그인은 확인했지만 서버의 비공개 추출 설정을 읽지 못했습니다. 설치된 비공개 폴더의 config.json 접근 권한과 PHP 경로 제한을 확인하세요. [service_not_configured]',
            'identity_conflict':'서버가 전달한 로그인 사용자 정보가 서로 다릅니다. 관리자에게 인증 전달 설정 확인을 요청하세요. [identity_conflict]',
            'insecure_php_configuration':'서버 PHP의 register_globals 설정 때문에 연결을 거부했습니다. 관리자에게 PHP 설정 확인을 요청하세요. [insecure_php_configuration]',
            'job_storage_failed':'서버 임시 추출 폴더에 쓸 수 없습니다. 실제 웹 실행 계정의 jobs 폴더 권한을 확인하세요. [job_storage_failed]',
            'secure_random_unavailable':'서버에서 추출 작업 번호를 안전하게 생성하지 못했습니다. 최신 서버 tools를 다시 설치하고 PHP·Python 실행 상태를 확인하세요. [secure_random_unavailable]',
            'csrf_required':'서버가 추출 요청의 인증 토큰을 확인하지 못했습니다. 서버 도구를 최신 버전으로 맞추고 다시 로그인하세요. [csrf_required]',
            'read_only':'서버가 추출 요청의 HTTP 메서드를 허용하지 않았습니다. PHP의 요청 전달 설정을 확인하세요. [read_only]',
            'unexpected_parameter':'서버가 지원하지 않는 추출 요청 항목을 받았습니다. Windows와 서버 도구의 버전을 맞추세요. [unexpected_parameter]',
            'empty_selection':'서버에 결재 문서·메일 중 선택한 수집 항목이 전달되지 않았습니다. [empty_selection]',
            'job_status_unavailable':'서버 추출 작업의 상태 파일을 읽지 못했습니다. [job_status_unavailable]',
            'job_not_found':'요청한 서버 추출 작업이 없거나 로그인 계정과 일치하지 않습니다. [job_not_found]',
            'invalid_job':'서버 추출 작업 번호가 올바르지 않습니다. [invalid_job]',
            'export_not_ready':'서버 추출 작업이 아직 완료되지 않았습니다. [export_not_ready]',
            'archive_unavailable':'완성된 서버 ZIP을 읽지 못했거나 크기가 달라졌습니다. [archive_unavailable]',
            'cleanup_failed':'서버 임시 ZIP 또는 작업 폴더를 삭제하지 못했습니다. 서버 정리 상태를 확인하세요. [cleanup_failed]',
            'native_login_required':'SEGIO 직원 로그인을 확인하지 못했습니다. ID·암호와 서버 인증 연결을 확인하세요.',
            'mail_path_unconfigured':'서버의 직원별 메일 저장 경로가 설정되지 않았습니다.',
            'mail_path_missing_or_symlink':'해당 직원의 메일 경로가 없거나 안전한 실제 경로가 아닙니다. 서버 설정을 확인하세요.',
            'mail_changed_retry':'서버 메일이 변경되어 안정적인 원본을 확보하지 못했습니다. 메일 정리·이동을 멈추고 다시 실행하거나 메일 사용이 적은 시간에 실행하세요.',
            'export_already_running':'이 직원의 추출 작업이 이미 진행 중입니다. 완료 후 다시 실행하세요.',
            'export_busy':'서버에서 다른 추출 작업이 진행 중입니다. 잠시 후 다시 실행하세요.',
            'export_process_unavailable':'서버 추출 작업기를 시작하지 못했습니다. Python·실행 계정 권한을 확인하세요.',
            'db_config_unreadable':'서버 작업기가 기존 db.conf를 읽지 못했습니다. segio 실행 계정의 파일 접근 권한을 확인하세요. [db_config_unreadable]',
            'db_config_invalid':'기존 db.conf의 접속 항목이 지원 형식과 다릅니다. 암호를 보내지 말고 관리자 진단을 실행하세요. [db_config_invalid]',
            'psql_unavailable':'서버 작업기가 psql을 실행하지 못했습니다. 설치 위치와 실행 권한을 확인하세요. [psql_unavailable]',
            'db_authentication_failed':'기존 db.conf로 PostgreSQL 인증에 실패했습니다. 직원 로그인 암호가 아닌 서버의 기존 DB 연결을 확인하세요. [db_authentication_failed]',
            'db_permission_denied':'DB 접속 계정에 결재 테이블 또는 스키마를 읽을 권한이 없습니다. 관리자 진단을 실행하세요. [db_permission_denied]',
            'db_schema_mismatch':'서버의 결재 DB 테이블·컬럼 구조가 추출기가 예상한 구조와 다릅니다. 관리자 진단을 실행하세요. [db_schema_mismatch]',
            'db_query_timeout':'서버의 읽기 전용 결재 DB 조회가 제한 시간을 초과했습니다. [db_query_timeout]',
            'db_database_missing':'설정된 cell_000 데이터베이스를 찾지 못했습니다. [db_database_missing]',
            'db_connection_failed':'서버 작업기에서 PostgreSQL에 접속하지 못했습니다. 기존 DB 서비스와 로컬 소켓 경로를 확인하세요. [db_connection_failed]',
            'db_query_failed':'서버 결재 DB 조회를 완료하지 못했습니다. 관리자 진단을 실행하세요. [db_query_failed]',
            'db_snapshot_mismatch':'결재 DB 조회 건수와 추출 결과가 일치하지 않습니다. [db_snapshot_mismatch]',
            'db_encoding_failed':'서버 결재 DB의 문자·파일명 인코딩을 처리하지 못했습니다. 최신 공용 tools를 설치하고 last-failure.json의 encoding·site 항목을 확인하세요. [db_encoding_failed]',
            'approval_user_not_found':'결재 DB에서 해당 직원의 프로필과 관련 문서를 찾지 못했습니다. 서버 연결 도구를 최신 버전으로 업데이트하세요. [approval_user_not_found]',
            'cell_data_missing':'서버의 셀 data 폴더가 없거나 작업기가 접근할 수 없습니다. [cell_data_missing]',
            'server_disk_full':'서버의 추출 작업용 디스크 공간이 부족합니다. [server_disk_full]',
            'server_file_too_large':'서버 파일 시스템 또는 실행 환경의 단일 파일 크기 제한을 초과했습니다. [server_file_too_large]',
            'server_access_denied':'서버 작업기가 원본 파일을 읽거나 작업 폴더에 쓸 권한이 없습니다. 관리자 진단을 실행하세요. [server_access_denied]',
            'server_file_missing':'추출 중 필요한 서버 파일 또는 폴더를 찾지 못했습니다. [server_file_missing]',
            'mail_unsafe_file':'메일 폴더에 일반 파일이 아닌 항목이 있어 수집을 중단했습니다. [mail_unsafe_file]',
            'mail_unsafe_symlink':'메일 폴더에 심볼릭 링크가 있어 수집을 중단했습니다. [mail_unsafe_symlink]',
            'cancelled':'DB·MBOX 가져오기를 중단했습니다.',
        }.get(code,'서버 추출 작업에서 처리 오류가 발생했습니다. 관리자 진단의 last_failure 항목을 확인하세요. [export_failed]')

    def manual_login_complete(self):
        # A disappearing/detached password frame is not authentication proof.
        candidate = getattr(self,'_last_login_base',None)
        if not candidate:
            pages = self.login_pages()
            candidate = next((urljoin(page.url,'/') for page in pages
                              if urljoin(page.url,'/').startswith('https://') and
                              origin(page.url) in self.scope),self.base)
        previous = self.base
        self.base = candidate
        try:
            info = self.live_call('info',pending_login=True,timeout_ms=min(self.config.timeout_ms,3000))
        except Exception:
            self.base = previous
            raise
        if info is None:
            self.base = previous
            return False
        self._verified_login_base = candidate
        return True

    def confirm_login(self):
        if getattr(self,'_verified_login_base',None):
            return
        deadline = time.monotonic()+self.config.timeout_ms/1000
        while time.monotonic()<deadline:
            self.checkpoint()
            if self.manual_login_complete():
                return
            self.page.wait_for_timeout(250)
        raise CollectionError('로그인 응답 뒤 서버가 본인 인증을 확인하지 못했습니다. live.php의 인증 전달과 서버 주소를 확인하세요. [LOGIN_NOT_CONFIRMED]')

    def poll_status(self):
        """Retry only read-only status requests for explicit transient failures."""
        from playwright.sync_api import Error, TimeoutError as BrowserTimeout
        for attempt in range(3):
            self.checkpoint()
            try:
                return self.live_call('status')
            except Exception as exc:
                transient = isinstance(exc,(BrowserTimeout,TimeoutError,ConnectionError))
                if isinstance(exc,Error) and re.search(r'\b(?:ERR_CONNECTION_RESET|ERR_CONNECTION_CLOSED|ECONNRESET)\b',str(exc)):
                    transient = True
                if isinstance(exc,BridgeResponseError):
                    data = exc.diagnostic
                    transient = data['http_status'] in (408,429,500,502,503,504) and data['body_kind'] not in ('login_form','php_error','php_source')
                if not transient or attempt==2:
                    raise
                self.log('서버 상태 조회의 일시적인 오류로 재시도합니다. (%d/2)'%(attempt+1))
                if self.config.cancel.wait(attempt+1):
                    self.checkpoint()

    def info(self):
        info = self.live_call('info')
        if not re.fullmatch(r'[a-f0-9]{64}',str(info.get('csrf',''))):
            raise CollectionError('서버의 요청 인증 정보를 확인하지 못했습니다.')
        if info.get('capabilities',{}).get('live_db') is not True:
            raise CollectionError('이 서버는 실시간 DB 추출을 지원하지 않습니다.')
        self.csrf = info['csrf']
        started = self.live_call('start', post=True, approvals='1' if self.config.approvals else '0',
                                 mail='1' if self.config.mail else '0')
        job = started.get('job','')
        if not re.fullmatch(r'[a-f0-9]{32}',str(job)):
            raise CollectionError('서버 작업 번호가 올바르지 않습니다.')
        self.job = job
        self.cleanup_confirmed = False
        self.cleanup_cancel_requested = False
        self.cleanup_attempts = 0
        self.cleanup_error_code = None
        previous = None
        deadline = time.monotonic()+72*3600
        while time.monotonic()<deadline:
            self.checkpoint()
            status = self.poll_status()
            if status.get('job')!=self.job:
                raise CollectionError('서버 작업 번호가 일치하지 않습니다.')
            phase = status.get('phase')
            measured = status.get('progress')
            measured = measured if isinstance(measured,dict) else {}
            self.progress(phase, measured.get('completed_bytes'), measured.get('total_bytes'),
                          attempt=measured.get('attempt'))
            if phase!=previous:
                self.log({'queued':'서버 추출 요청 접수', 'database':'로그인한 직원의 DB 자료를 읽는 중',
                          'mail':'서버 MBOX 원본 전체를 복사하는 중', 'packing':'전송 자료를 압축하는 중',
                          'hashing':'서버 전송 ZIP의 무결성을 검증하는 중',
                          'ready':'이번 요청의 DB·MBOX 추출 완료'}.get(phase,'서버 추출 작업 확인 중'))
                previous = phase
            if status.get('state')=='failed':
                stages = {'initializing':'서버 작업 준비', 'server_request':'서버 추출 요청', 'database':'서버 결재 DB 추출',
                          'mail':'서버 MBOX 원본 복사', 'packing':'서버 ZIP 압축',
                          'hashing':'서버 ZIP 검증'}
                diagnostic = status.get('diagnostic')
                stage = stages.get(diagnostic.get('stage')) if isinstance(diagnostic,dict) else None
                raise CollectionError((stage+' 단계: ' if stage else '')+self.error_message(phase))
            if status.get('state')=='ready':
                if (type(status.get('bytes')) is not int or status['bytes']<=0 or
                    not re.fullmatch(r'[a-f0-9]{64}',str(status.get('sha256','')))):
                    raise CollectionError('추출 파일의 검증 정보가 올바르지 않습니다.')
                return status
            self.config.cancel.wait(1)
        raise CollectionError('서버 추출 대기 시간이 초과되었습니다.')

    def download_endpoint(self, record):
        return urljoin(self.base, LIVE_BRIDGE+'?'+urlencode({'action':'download','job':self.job}))

    def validate_download(self, destination):
        manifest = json.loads((destination/'manifest.json').read_text(encoding='utf-8'))
        live = manifest.get('live_export',{})
        if (live.get('job')!=self.job or live.get('approvals')!=self.config.approvals or
            live.get('mail')!=self.config.mail):
            raise CollectionError('이번 요청의 DB·MBOX 선택과 추출 결과가 일치하지 않습니다.')

    def finalize_download(self, root, archive, destination):
        from .mail_import import build_local_catalog
        self.phase = 'Windows DB·메일 검색 색인 생성'
        self.log('Windows에 개인 DB와 메일 제목 검색 색인을 만드는 중')
        build_local_catalog(destination, self.config.username, self.checkpoint, self.log, self.progress)

    def cleanup_remote(self, attempt_limit=3):
        if self.cleanup_confirmed or self.cleanup_cancel_requested or not self.job or not self.csrf:
            return
        available = min(attempt_limit, 3-self.cleanup_attempts)
        if available <= 0:
            raise CollectionError('서버 임시 ZIP 삭제 요청을 세 번 시도했지만 확인하지 못했습니다.')
        phase = self.phase
        try:
            for attempt in range(available):
                self.cleanup_attempts += 1
                try:
                    # Extracting a large mailbox can outlive the hourly CSRF
                    # window. Refresh identity and token without starting a job.
                    info = self.live_call('info',timeout_ms=10000)
                    token = info.get('csrf','')
                    if not re.fullmatch(r'[a-f0-9]{64}',str(token)):
                        raise CollectionError('서버 정리용 인증 정보를 확인하지 못했습니다.')
                    self.csrf = token
                    result = self.live_call('cleanup',post=True,timeout_ms=10000)
                    if result.get('job')==self.job and result.get('state')=='cancellation_requested':
                        self.cleanup_cancel_requested = True
                        return
                    if (result.get('job')!=self.job or result.get('state')!='cleaned' or
                            result.get('archive_deleted') is not True or result.get('job_deleted') is not True):
                        raise CollectionError('서버 임시 ZIP 삭제 확인을 받지 못했습니다. 서버 도구를 갱신하고 정리 상태를 확인하세요.')
                    self.cleanup_confirmed = True
                    self.cleanup_error_code = None
                    self.log('서버 임시 ZIP과 이번 작업 폴더 삭제를 확인했습니다.')
                    return
                except Exception as exc:
                    error = exc if isinstance(exc,CollectionError) else processing_error(exc,'서버 임시 ZIP 삭제 확인')
                    matched = re.search(r'\[([A-Za-z0-9_]+)\]',str(error))
                    allowed = {'cleanup_failed','csrf_required','BRIDGE_NOT_JSON','BRIDGE_INVALID_JSON',
                               'BRIDGE_LOGIN_REQUIRED','TIMEOUT','ERR_CONNECTION_RESET',
                               'ERR_CONNECTION_CLOSED','BROWSER_CONNECTION_ERROR'}
                    self.cleanup_error_code = matched[1] if matched and matched[1] in allowed else 'CLEANUP_UNCONFIRMED'
                    if attempt==available-1:
                        raise
                    time.sleep(.25*(attempt+1))
        finally:
            self.phase = phase

    def save_cleanup_status(self, destination):
        from .archive import atomic_write
        path = destination/'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['server_cleanup'] = {'state':'deleted' if self.cleanup_confirmed else 'pending',
                                      'archive_deleted':self.cleanup_confirmed}
        if not self.cleanup_confirmed and self.cleanup_error_code:
            manifest['server_cleanup']['error'] = self.cleanup_error_code
        atomic_write(path,json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))

    def after_verified_download(self, destination):
        self.progress('cleanup')
        try:
            self.cleanup_remote(attempt_limit=1)
        except Exception:
            self.log('서버 임시 ZIP 삭제를 아직 확인하지 못했습니다. 로컬 처리 후 다시 확인합니다.')
        self.save_cleanup_status(destination)

    def finish_remote_cleanup(self, destination):
        try:
            self.cleanup_remote()
        except Exception:
            self.log('주의: PC 백업은 저장됐지만 서버 임시 ZIP 삭제는 확인되지 않았습니다.')
        self.save_cleanup_status(destination)
