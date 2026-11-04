"""Per-user Windows URL handler. It opens a viewer; URLs never authorize deletion."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit,parse_qs,unquote

SCHEME='segio-backup'


def register():
    if os.name!='nt':return False
    import winreg
    if getattr(sys,'frozen',False):
        command=subprocess.list2cmdline([sys.executable,'manage-url'])+' "%1"'
    else:
        python=Path(sys.executable).with_name('pythonw.exe')
        if not python.is_file():python=Path(sys.executable)
        launcher=Path(__file__).resolve()
        command=subprocess.list2cmdline([str(python),str(launcher),'manage-url'])+' "%1"'
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Software\\Classes\\'+SCHEME) as key:
            winreg.SetValueEx(key,'',0,winreg.REG_SZ,'URL:SEGIO 개인 백업 관리')
            winreg.SetValueEx(key,'URL Protocol',0,winreg.REG_SZ,'')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Software\\Classes\\'+SCHEME+'\\shell\\open\\command') as key:
            winreg.SetValueEx(key,'',0,winreg.REG_SZ,command)
        return True
    except OSError:return False


def parse_url(value):
    parsed=urlsplit(value)
    fields=parse_qs(parsed.query,strict_parsing=True)
    if parsed.scheme!=SCHEME or parsed.netloc!='open' or parsed.path not in ('','/') or set(fields)!={'archive','token','port'}:
        raise ValueError('백업 관리 연결 주소가 올바르지 않습니다.')
    if any(len(values)!=1 for values in fields.values()):raise ValueError('연결 매개변수가 중복되었습니다.')
    token=fields['token'][0]
    if not re.fullmatch('[a-f0-9]{64}',token):raise ValueError('연결 토큰이 올바르지 않습니다.')
    port=int(fields['port'][0])
    if not 49152<=port<=65000:raise ValueError('연결 포트가 올바르지 않습니다.')
    archive=urlsplit(fields['archive'][0])
    if archive.scheme!='file' or archive.netloc or archive.query or archive.fragment:
        raise ValueError('PC 안의 백업 index.html만 열 수 있습니다.')
    path=unquote(archive.path)
    if not path.startswith('/') or '\x00' in path:raise ValueError('절대 경로의 백업 index.html을 선택하세요.')
    if os.name=='nt':
        if not re.match(r'^/[A-Za-z]:/',path) or '\\' in path or '\x00' in path:raise ValueError('네트워크 경로는 지원하지 않습니다.')
        path=path[1:]
    folder=Path(path).parent.resolve()
    if Path(path).name.lower()!='index.html':raise ValueError('백업 index.html을 선택하세요.')
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!='segio-db-html/1' or not isinstance(manifest.get('live_export'),dict):
        raise ValueError('개인 로그인으로 완료한 백업만 관리할 수 있습니다.')
    return folder,token,port


def run_url(value):
    from .viewer import Viewer
    folder,token,port=parse_url(value)
    viewer=None
    for offset in range(8):
        try:
            viewer=Viewer(folder,bridge_token=token,port=port+offset,bridge=True)
            break
        except OSError:
            continue
    if viewer is None:raise ValueError('PC 관리 연결 포트를 사용할 수 없습니다. 다시 연결하세요.')
    try:
        # An idle helper exits, while a confirmed deletion is allowed to finish.
        while time.monotonic()-viewer.last_activity<1800 or viewer.edit_running:
            threading.Event().wait(1)
    finally:viewer.close()


if __name__=='__main__':
    # ShellExecute does not inherit the source checkout's working directory.
    # This absolute entry point also exists in an installed Python package.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from segio_archive.__main__ import main
    raise SystemExit(main())
