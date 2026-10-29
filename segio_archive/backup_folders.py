"""Locate completed local backups without treating diagnostics as backup data."""
import hashlib
import json
from pathlib import Path

from .web import CollectionError


BASELINE_REASONS = {
    'account_mismatch':'선택한 백업의 그룹웨어 ID가 입력한 ID와 다릅니다. 본인의 백업 폴더와 그룹웨어 ID를 확인하세요.',
    'incomplete':'선택한 백업이 정상 완료 상태가 아닙니다. ‘기존 백업 갱신’으로 처리를 완료하거나 정상 완료한 백업을 선택하세요.',
    'manifest_invalid':'기존 백업의 manifest.json 형식 또는 필수 항목을 확인하지 못했습니다. 정상 완료한 백업 폴더를 선택하세요.',
    'manifest_json_invalid':'기존 백업의 manifest.json을 JSON으로 읽지 못했습니다. 백업 폴더를 보존하고 오류 기록을 확인하세요. 파일 내용을 임의로 변경하지 마세요.',
    'manifest_format_unsupported':'선택한 manifest.json은 개인 DB·메일 백업 형식이 아닙니다. 이 프로그램으로 완료한 백업 폴더를 선택하세요.',
    'live_export_missing':'선택한 백업에 개인 로그인 추출 정보가 없습니다. 개인 로그인 방식으로 완료한 백업 폴더를 선택하세요.',
    'manifest_files_invalid':'기존 백업의 manifest.json에 파일 검증 목록이 없거나 형식이 다릅니다. 원본 폴더를 보존하고 정상 완료한 백업을 선택하세요.',
    'mail_list_changed':'메일 원본 목록의 검증 정보가 일치하지 않습니다. ‘파일 무결성 검증’으로 기존 백업을 확인하세요.',
    'mail_size_changed':'기존 메일 원본의 크기가 검증 기록과 다릅니다. ‘파일 무결성 검증’으로 기존 백업을 확인하세요.',
    'catalog_changed':'결재 목록 catalog.json의 검증 정보가 일치하지 않습니다. ‘파일 무결성 검증’으로 기존 백업을 확인하세요.',
    'database_changed':'검색 DB catalog.sqlite3의 검증 정보가 일치하지 않습니다. 기존 백업을 보존하고 ‘파일 무결성 검증’으로 확인하세요.',
    'time_unavailable':'이전 백업의 기준 시각을 확인하지 못했습니다. 기준 시각이 있는 정상 완료 백업을 선택하거나 새 전체 백업을 진행하세요.',
    'viewer_not_ready':'기존 백업의 검색 색인이 준비되지 않았습니다. ‘기존 백업 갱신’으로 처리를 완료하세요.',
    'portable_not_ready':'기존 백업의 메일 바로보기가 준비되지 않았습니다. ‘기존 백업 갱신’으로 처리를 완료하세요.',
    'unsafe_path':'이전 백업에 안전한 보관 경로로 확인할 수 없는 항목이 있습니다. 원본을 유지하고 백업 폴더를 확인하세요.',
    'backup_locked':'기존 백업의 반영 잠금을 확보하지 못했습니다. 다른 백업 작업의 실행 상태와 폴더 접근 권한을 확인하세요.',
    'interrupted_update':'이전 백업 반영이 중단된 기록이 있습니다. 기존 폴더와 .segio-previous- 폴더를 보존하고 PC 폴더 접근 상태를 확인하세요.',
}


class BaselineValidationError(CollectionError):
    def __init__(self, reason, *, metadata=None):
        self.baseline_diagnostic={'reason':reason}
        if metadata:
            self.baseline_diagnostic.update(metadata)
        super().__init__(BASELINE_REASONS[reason]+' [BASELINE_INVALID]')


def read_backup_metadata(path, checkpoint=lambda: None, *, kind='manifest'):
    """Read metadata without a fixed size cap; hash exactly the bytes being parsed."""
    invalid='manifest_json_invalid' if kind=='manifest' else 'mail_list_changed'
    if path.is_symlink():
        raise BaselineValidationError('unsafe_path')
    data=bytearray()
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            checkpoint()
            block=stream.read(1024*1024)
            if not block:
                break
            digest.update(block)
            data.extend(block)
    try:
        value=json.loads(data)
    except json.JSONDecodeError as exc:
        raise BaselineValidationError(invalid,metadata={
            'metadata_kind':kind,'metadata_bytes':len(data),
            'json_line':exc.lineno,'json_column':exc.colno}) from None
    except (UnicodeError,ValueError,RecursionError):
        raise BaselineValidationError(invalid,metadata={
            'metadata_kind':kind,'metadata_bytes':len(data)}) from None
    if not isinstance(value,dict):
        raise BaselineValidationError(invalid)
    return value,{'bytes':len(data),'sha256':digest.hexdigest()}


def validate_baseline_manifest(manifest, user):
    if manifest.get('user')!=user:
        raise BaselineValidationError('account_mismatch')
    if manifest.get('format')!='segio-db-html/1':
        raise BaselineValidationError('manifest_format_unsupported')
    if not isinstance(manifest.get('live_export'),dict):
        raise BaselineValidationError('live_export_missing')
    if not isinstance(manifest.get('files'),dict):
        raise BaselineValidationError('manifest_files_invalid')
    processing=manifest.get('local_processing',{})
    if not isinstance(processing,dict):
        raise BaselineValidationError('manifest_invalid')
    if (manifest.get('state') not in ('collected','collected_with_warnings') or
            processing.get('state')=='pending'):
        raise BaselineValidationError('incomplete')


class MissingBackupFile(CollectionError):
    def __init__(self, root, relative):
        key = {'manifest.json':'manifest', 'catalog.json':'catalog',
               'catalog.sqlite3':'search_database', 'mail/sources.json':'mail_sources'}.get(relative)
        if key is None:
            key = 'mail_raw' if relative.startswith('mail/raw/') else 'backup_asset'
        self.baseline_diagnostic = {'required_file':key}
        super().__init__('추가 백업에 필요한 파일이 없습니다: '+str(Path(root)/relative)+
            '\nindex.html과 manifest.json이 있는 실제 백업 폴더를 선택하세요. '
            '필수 원본이 삭제된 백업에는 추가할 수 없습니다. '
            'last-client-failure.json은 오류 기록이며 추가 백업에 필요하지 않습니다. [BASELINE_FILE_MISSING]')


def require_backup_file(root, relative):
    path = Path(root)/relative
    try:
        path.stat()
    except FileNotFoundError:
        raise MissingBackupFile(root, relative) from None
    return path


def backup_candidates(parent, user=''):
    """Only inspect immediate child manifests; never scan raw mail or attachments."""
    parent = Path(parent).expanduser().resolve()
    found = []
    for path in parent.iterdir():
        if path.is_symlink() or not path.is_dir() or path.name.startswith('.'):
            continue
        manifest = path/'manifest.json'
        try:
            value,_ = read_backup_metadata(manifest)
            if (value.get('format') == 'segio-db-html/1' and
                    value.get('state') in ('collected','collected_with_warnings') and
                    value.get('local_processing',{}).get('state') != 'pending' and
                    (not user or value.get('user') == user)):
                found.append((str(value.get('finished_at','')),path.name,path))
            del value
        except (OSError, ValueError, TypeError, AttributeError, CollectionError):
            continue
    return [item[2] for item in sorted(found,reverse=True)]
