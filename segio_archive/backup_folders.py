"""Locate completed local backups without treating diagnostics as backup data."""
import json
from pathlib import Path

from .web import CollectionError


BASELINE_REASONS = {
    'account_mismatch':'선택한 백업의 그룹웨어 ID가 입력한 ID와 다릅니다. 본인의 백업 폴더와 그룹웨어 ID를 확인하세요.',
    'incomplete':'선택한 백업이 정상 완료 상태가 아닙니다. ‘기존 백업 갱신’으로 처리를 완료하거나 정상 완료한 백업을 선택하세요.',
    'manifest_invalid':'기존 백업의 manifest.json 형식 또는 필수 항목을 확인하지 못했습니다. 정상 완료한 백업 폴더를 선택하세요.',
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
    def __init__(self, reason):
        self.baseline_diagnostic={'reason':reason}
        super().__init__(BASELINE_REASONS[reason]+' [BASELINE_INVALID]')


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
            if manifest.is_symlink() or manifest.stat().st_size > 16*1024*1024:
                continue
            value = json.loads(manifest.read_bytes())
            if (value.get('format') == 'segio-db-html/1' and
                    value.get('state') in ('collected','collected_with_warnings') and
                    value.get('local_processing',{}).get('state') != 'pending' and
                    (not user or value.get('user') == user)):
                found.append((str(value.get('finished_at','')),path.name,path))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return [item[2] for item in sorted(found,reverse=True)]
