"""Small allowlisted client diagnostics; never retain HTTP bodies or credentials."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from . import __version__
from .browser import BrowserUnavailable
from .web import Cancelled, CollectionError

ACTIONS = {'info','start','status','download','cleanup'}
MEDIA = {'application/json','text/html','text/plain','application/octet-stream','application/x-httpd-php'}
BODY_KINDS = {'empty','login_form','php_source','php_error','html','json_like','other'}
PHASES = {'시작','브라우저 시작','SEGIO 로그인','서버 연결 기능 확인','서버 로그인 확인',
          '서버 추출 요청','서버 추출 상태 확인','서버 연결','다운로드 저장 폴더 준비',
          '보관 ZIP 다운로드','내려받은 파일 검증','Windows DB·메일 검색 색인 생성'}
ERROR_CODES = {'BRIDGE_NOT_JSON','BRIDGE_INVALID_JSON','BRIDGE_RESPONSE_TOO_LARGE',
               'service_not_configured','job_not_found','BRIDGE_STATUS_NOT_FOUND','invalid_baseline',
               'BRIDGE_NOT_INSTALLED','BRIDGE_LOGIN_REQUIRED','BRIDGE_PHP_SOURCE',
               'LOGIN_ORIGIN_MISMATCH','LOGIN_NOT_CONFIRMED','BRIDGE_REDIRECT',
               'TIMEOUT','LOGIN_FRAME_TRANSITION','BROWSER_CONNECTION_ERROR',
               'FILE_ACCESS_DENIED','FILE_IO_ERROR','DISK_FULL','FILE_TOO_LARGE',
               'UNEXPECTED_PROCESSING_ERROR','INVALID_JSON','LOCAL_INDEX_PENDING',
               'CANCELLED','BROWSER_UNAVAILABLE','COLLECTION_ERROR',
               'ERR_CONNECTION_REFUSED','ERR_CONNECTION_RESET','ERR_CONNECTION_CLOSED',
               'ERR_NAME_NOT_RESOLVED','ERR_CERT_AUTHORITY_INVALID','ERR_CERT_DATE_INVALID',
               'ERR_SSL_PROTOCOL_ERROR','ERR_SSL_VERSION_OR_CIPHER_MISMATCH','ERR_FAILED',
               'ECONNREFUSED','ECONNRESET','ENOTFOUND','CERT_HAS_EXPIRED'}
SERVICE_CHECKS = {'config_exists','config_readable','config_json_object',
                  'php_path_restriction','php_safe_mode'}
BRIDGE_STATES = {'available','not_found','login_required','service_not_configured','unavailable'}


def safe_response(data):
    return {
        'action':data.get('action') if data.get('action') in ACTIONS else 'unknown',
        'http_status':data.get('http_status') if type(data.get('http_status')) is int and 100<=data['http_status']<=599 else None,
        'content_type':data.get('content_type') if data.get('content_type') in MEDIA|{'missing','other'} else 'other',
        'body_kind':data.get('body_kind') if data.get('body_kind') in BODY_KINDS else 'other'}


def response_summary(action, status, content_type, body=b''):
    media = content_type.split(';',1)[0].strip().lower()
    sample = body[:8192].lstrip()
    if not sample:
        kind = 'empty'
    elif b'<?php' in sample.lower():
        kind = 'php_source'
    elif re.search(rb'<input\b[^>]*\btype\s*=\s*[\'"]?password\b',sample,re.I):
        kind = 'login_form'
    elif re.search(rb'(?:fatal|parse)\s+error\b|(?:warning|notice)\s*:',sample,re.I):
        kind = 'php_error'
    elif re.search(rb'<(?:!doctype|html|head|body|script)\b',sample,re.I):
        kind = 'html'
    elif sample[:1] in (b'{',b'['):
        kind = 'json_like'
    else:
        kind = 'other'
    return {'action':action if action in ACTIONS else 'unknown',
            'http_status':status if type(status) is int and 100<=status<=599 else None,
            'content_type':media if media in MEDIA else 'missing' if not media else 'other',
            'body_kind':kind}


class BridgeResponseError(CollectionError):
    def __init__(self, message, code, diagnostic):
        self.code = code
        self.diagnostic = diagnostic
        action = {'info':'로그인 확인','start':'추출 요청','status':'상태 조회',
                  'download':'다운로드','cleanup':'작업 정리'}.get(diagnostic['action'],'서버 연결')
        super().__init__('%s (요청: %s, HTTP %s, 형식: %s) [%s]' %
                         (message,action,diagnostic['http_status'],diagnostic['content_type'],code))


class ServiceConfigurationError(BridgeResponseError):
    def __init__(self, diagnostic, checks):
        self.server_checks = {key:value for key,value in checks.items()
                              if key in SERVICE_CHECKS and type(value) is bool} if isinstance(checks,dict) else {}
        if self.server_checks.get('config_exists') is False:
            hint = '서버 설정 파일을 확인하지 못했습니다. 설치 경로·파일 누락과 PHP 접근 제한을 확인하세요.'
        elif self.server_checks.get('config_readable') is False:
            hint = '서버 PHP가 설정 파일을 읽지 못했습니다. 실행 계정의 접근 권한과 PHP 경로 제한을 확인하세요.'
        elif self.server_checks.get('config_json_object') is False:
            hint = '서버 설정 파일을 JSON으로 해석하지 못했습니다. 설정 파일 형식을 확인하세요.'
        else:
            hint = '서버 추출 설정을 확인하지 못했습니다. 설치 경로와 필수 설정 항목을 확인하세요.'
        super().__init__(hint,'service_not_configured',diagnostic)


def write_failure(output, exception, phase):
    code = 'CANCELLED' if isinstance(exception,Cancelled) else 'BROWSER_UNAVAILABLE' if isinstance(exception,BrowserUnavailable) else 'COLLECTION_ERROR'
    matched = re.search(r'\[([A-Za-z0-9_]+)\]',str(exception))
    if matched and matched[1] in ERROR_CODES:
        code = matched[1]
    record = {'protocol':'segio-client-diagnostic/1','program_version':__version__,
              'failed_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'phase':phase if phase in PHASES else 'unknown','error':code}
    if isinstance(exception,BridgeResponseError):
        record['response'] = safe_response(exception.diagnostic)
    checks = getattr(exception,'status_checks',None)
    if isinstance(checks,dict):
        record['status_checks'] = {
            'attempts':checks.get('attempts') if type(checks.get('attempts')) is int and 1<=checks['attempts']<=3 else None,
            'bridge_state':checks.get('bridge_state') if checks.get('bridge_state') in BRIDGE_STATES else 'unavailable'}
        if isinstance(checks.get('bridge_response'),dict):
            record['status_checks']['bridge_response'] = safe_response(checks['bridge_response'])
    checks = getattr(exception,'server_checks',None)
    if isinstance(checks,dict):
        record['server_checks'] = {key:value for key,value in checks.items()
                                   if key in SERVICE_CHECKS and type(value) is bool}
    data = getattr(exception,'filesystem_diagnostic',None)
    if isinstance(data,dict):
        record['filesystem'] = {
            'exception_type':data.get('exception_type') if data.get('exception_type') in (
                'PermissionError','FileNotFoundError','FileExistsError','OSError','IsADirectoryError','NotADirectoryError') else 'OSError',
            'errno':data.get('errno') if type(data.get('errno')) is int else None,
            'winerror':data.get('winerror') if type(data.get('winerror')) is int else None,
            'operation':data.get('operation') if data.get('operation') in ('replace','file_io') else 'file_io'}
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True,exist_ok=True)
    destination = output/'last-client-failure.json'
    descriptor, temporary = tempfile.mkstemp(prefix='.segio-client-error-',dir=output)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(json.dumps(record,ensure_ascii=False,indent=2).encode('utf-8'))
        os.replace(temporary,destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination
