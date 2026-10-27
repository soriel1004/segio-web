"""Locate completed local backups without treating diagnostics as backup data."""
import json
from pathlib import Path

from .web import CollectionError


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
