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


def write_failure(output, exception, phase):
    code = 'CANCELLED' if isinstance(exception,Cancelled) else 'BROWSER_UNAVAILABLE' if isinstance(exception,BrowserUnavailable) else 'COLLECTION_ERROR'
    matched = re.search(r'\[([A-Za-z0-9_]+)\]',str(exception))
    if matched and matched[1] in ERROR_CODES:
        code = matched[1]
    record = {'protocol':'segio-client-diagnostic/1','program_version':__version__,
              'failed_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'phase':phase if phase in PHASES else 'unknown','error':code}
    if isinstance(exception,BridgeResponseError):
        data = exception.diagnostic
        record['response'] = {
            'action':data.get('action') if data.get('action') in ACTIONS else 'unknown',
            'http_status':data.get('http_status') if type(data.get('http_status')) is int and 100<=data['http_status']<=599 else None,
            'content_type':data.get('content_type') if data.get('content_type') in MEDIA|{'missing','other'} else 'other',
            'body_kind':data.get('body_kind') if data.get('body_kind') in BODY_KINDS else 'other'}
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
