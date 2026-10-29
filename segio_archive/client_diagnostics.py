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
          '보관 ZIP 다운로드','내려받은 파일 검증','Windows DB·메일 검색 색인 생성',
          '이전 백업 확인','기존 백업 반영 준비'}
ERROR_CODES = {'BRIDGE_NOT_JSON','BRIDGE_INVALID_JSON','BRIDGE_RESPONSE_TOO_LARGE',
               'service_not_configured','job_not_found','BRIDGE_STATUS_NOT_FOUND','invalid_baseline','invalid_approval_since',
               'BRIDGE_NOT_INSTALLED','BRIDGE_LOGIN_REQUIRED','BRIDGE_PHP_SOURCE',
               'LOGIN_ORIGIN_MISMATCH','LOGIN_NOT_CONFIRMED','BRIDGE_REDIRECT',
               'TIMEOUT','LOGIN_FRAME_TRANSITION','BROWSER_CONNECTION_ERROR',
               'FILE_ACCESS_DENIED','FILE_IO_ERROR','FILE_SECURITY_BLOCKED','DISK_FULL','FILE_TOO_LARGE',
               'UNEXPECTED_PROCESSING_ERROR','INVALID_JSON','LOCAL_INDEX_PENDING',
               'LOCAL_DATABASE_ERROR','LOCAL_TEXT_ENCODING_ERROR','MEMORY_LIMIT','MAIL_FORMAT_ERROR',
               'CANCELLED','BROWSER_UNAVAILABLE','COLLECTION_ERROR','BASELINE_FILE_MISSING','BASELINE_INVALID',
               'ERR_CONNECTION_REFUSED','ERR_CONNECTION_RESET','ERR_CONNECTION_CLOSED',
               'ERR_NAME_NOT_RESOLVED','ERR_CERT_AUTHORITY_INVALID','ERR_CERT_DATE_INVALID',
               'ERR_SSL_PROTOCOL_ERROR','ERR_SSL_VERSION_OR_CIPHER_MISMATCH','ERR_FAILED',
               'ECONNREFUSED','ECONNRESET','ENOTFOUND','CERT_HAS_EXPIRED'}
SERVICE_CHECKS = {'config_exists','config_readable','config_json_object',
                  'php_path_restriction','php_safe_mode'}
BRIDGE_STATES = {'available','not_found','login_required','service_not_configured','unavailable'}
PROCESSING_TYPES = {'HeaderParseError','MessageError','Error','UnicodeEncodeError','UnicodeDecodeError',
                    'UnicodeError','ValueError','TypeError','AttributeError','KeyError','IndexError',
                    'RecursionError','MemoryError','OperationalError','IntegrityError','DatabaseError',
                    'ParserRejectedMarkup','RuntimeError','AssertionError','PermissionError','OSError'}
PROCESSING_SITES = {
    'mail_import':{'header_text','decode_bytes','build_local_catalog','reconcile_mail_source'},
    'compact_mail':{'build_compact_catalog','read_headers','mbox_spans','digest_file','search'},
    'portable_mail':{'render_record','render_message','render_raw_record','mime_parts','headers','decoded',
                     'safe_html','sanitized_html','write_generated','build_portable_mail','prepare_archive',
                     'local_path','add','__enter__','__exit__','open'},
    'search_page':{'install_search_page','write','script_json'},
    'document_page':{'upgrade_documents'},
    'index_metadata':{'__init__','item_date'},
    'mail_folders':{'__init__','info','boxes'},
    'db_client':{'receive','finalize_download','unpack_verified'},
    'attachment_safety':{'attachment_io','__init__','record'},
    'date_incremental':{'__init__','prepare','copy','commit','_commit','recover'},
    'incremental':{'__init__','raw_path'},
    'file_ops':{'replace_file','remove_file','retry_file_io','merge_io'},
}


def safe_processing(data):
    kind=data.get('exception_type')
    result={'exception_type':kind if isinstance(kind,str) and kind in PROCESSING_TYPES else 'Exception'}
    site=data.get('site')
    if (isinstance(site,dict) and isinstance(site.get('module'),str) and site['module'] in PROCESSING_SITES and
            isinstance(site.get('function'),str) and site['function'] in PROCESSING_SITES[site['module']] and
            type(site.get('line')) is int and 1<=site['line']<=10000000):
        result['site']={key:site[key] for key in ('module','function','line')}
    if type(data.get('sqlite_errorcode')) is int and 0<=data['sqlite_errorcode']<=0x7fffffff:
        result['sqlite_errorcode']=data['sqlite_errorcode']
    return result


def processing_summary(exception):
    data={'exception_type':type(exception).__name__}
    trace=exception.__traceback__
    while trace is not None:
        frame=trace.tb_frame
        module=frame.f_globals.get('__name__','')
        key=module.removeprefix('segio_archive.')
        function=frame.f_code.co_name
        if module=='segio_archive.'+key and key in PROCESSING_SITES and function in PROCESSING_SITES[key]:
            data['site']={'module':key,'function':function,'line':trace.tb_lineno}
        trace=trace.tb_next
    code=getattr(exception,'sqlite_errorcode',None)
    if type(code) is int:data['sqlite_errorcode']=code
    return safe_processing(data)


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
    data=getattr(exception,'processing_diagnostic',None)
    if isinstance(data,dict):record['processing']=safe_processing(data)
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
    data = getattr(exception,'baseline_diagnostic',None)
    if isinstance(data,dict):
        from .backup_folders import BASELINE_REASONS
        baseline={}
        if data.get('required_file') in ('manifest','catalog','search_database','mail_sources','mail_raw','backup_asset'):
            baseline['required_file']=data['required_file']
        if isinstance(data.get('reason'),str) and data['reason'] in BASELINE_REASONS:
            baseline['reason']=data['reason']
        if data.get('metadata_kind') in ('manifest','mail_sources'):
            baseline['metadata_kind']=data['metadata_kind']
        for key in ('metadata_bytes','json_line','json_column'):
            if type(data.get(key)) is int and 0<=data[key]<=0x7fffffffffffffff:
                baseline[key]=data[key]
        if baseline:record['baseline']=baseline
    data = getattr(exception,'merge_diagnostic',None)
    if isinstance(data,dict):
        from .file_ops import MERGE_OPERATIONS, MERGE_KINDS
        if (isinstance(data.get('operation'),str) and data['operation'] in MERGE_OPERATIONS and
                isinstance(data.get('file_kind'),str) and data['file_kind'] in MERGE_KINDS):
            record['merge']={key:data[key] for key in ('operation','file_kind')}
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


def failure_notice(output, exception, phase):
    try:
        return '\n\n오류 기록: '+str(write_failure(output,exception,phase))
    except OSError:
        return '\n\n지정한 Windows 저장 폴더에 오류 기록을 저장하지 못했습니다. 폴더 접근 권한과 여유 공간을 확인하세요.'
