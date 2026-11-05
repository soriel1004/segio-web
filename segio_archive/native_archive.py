"""Per-user Windows URL handler. It opens a viewer; URLs never authorize deletion."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit,parse_qs,unquote,urlencode

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
    if port!=0 and not 49152<=port<=65000:raise ValueError('연결 포트가 올바르지 않습니다.')
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
    from .delete_transaction import journal_path,recover
    if journal_path(folder).exists():
        from .date_incremental import update_lock
        with update_lock(folder):recover(folder)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!='segio-db-html/1' or not isinstance(manifest.get('live_export'),dict):
        raise ValueError('개인 로그인으로 완료한 백업만 관리할 수 있습니다.')
    return folder,token,port


def signal_path(folder,token):
    """A public readiness signal; never publish the private viewer capability."""
    if not re.fullmatch('[a-f0-9]{64}',token):raise ValueError('연결 토큰이 올바르지 않습니다.')
    search=folder/'search'
    if search.is_symlink() or search.resolve().parent!=folder.resolve():
        raise ValueError('백업 검색 폴더의 경로를 확인하세요.')
    return search/('manage-'+token+'.js')


def publish_signal(path,token,state,**fields):
    data={'protocol':'segio-manage-ready/1','token':token,'state':state,**fields}
    payload='window.segioManageReady('+json.dumps(data,ensure_ascii=True).replace('<','\\u003c')+');'
    if path.is_symlink():raise ValueError('관리 연결 파일의 경로를 확인하세요.')
    descriptor,temporary=tempfile.mkstemp(prefix='.manage-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as output:output.write(payload)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def parse_view_url(value):
    parsed=urlsplit(value)
    fields=parse_qs(parsed.query,strict_parsing=True)
    if (parsed.scheme!=SCHEME or parsed.netloc!='view' or parsed.path not in ('','/') or parsed.fragment or
            set(fields)!={'archive'} or len(fields['archive'])!=1):
        raise ValueError('보관함 실행 주소가 올바르지 않습니다.')
    folder,_,_=parse_url(SCHEME+'://open?'+urlencode({'archive':fields['archive'][0],'token':secrets.token_hex(32),'port':0}))
    return folder


def run_view(value):
    import webbrowser
    from .viewer import Viewer
    viewer=Viewer(parse_view_url(value))
    try:
        if not webbrowser.open(viewer.url):raise ValueError('보관함을 브라우저에서 열지 못했습니다. 프로그램의 보관함 열기를 사용하세요.')
        while time.monotonic()-viewer.last_activity<1800 or viewer.edit_running:
            threading.Event().wait(1)
    finally:viewer.close()


def run_url(value):
    if urlsplit(value).netloc=='view':return run_view(value)
    folder,token,port=parse_url(value)
    signal=signal_path(folder,token)
    viewer=None
    try:
        from .viewer import Viewer
        # New indexes request port 0: the OS chooses a free port and the file
        # signal announces it only after the server is listening. Keep old
        # explicitly requested ports for an already-open v0.7.0 index.
        for offset in range(8 if port else 1):
            try:
                viewer=Viewer(folder,bridge_token=token,port=port+offset,bridge=True)
                break
            except OSError as exc:
                import errno
                if exc.errno!=errno.EADDRINUSE and getattr(exc,'winerror',None)!=10048:raise
        if viewer is None:raise OSError('PC 관리 연결 포트를 사용할 수 없습니다.')
        publish_signal(signal,token,'ready',port=viewer.server.server_port)
        # An idle helper exits, while a confirmed deletion is allowed to finish.
        started=viewer.last_activity
        while (time.monotonic()-viewer.last_activity<(120 if viewer.last_activity==started else 1800)
               or viewer.edit_running):
            threading.Event().wait(1)
    except Exception as exc:
        message='PC 삭제 관리 프로그램을 준비하지 못했습니다. 새 프로그램의 보관함 열기로 백업을 다시 열어주세요.'
        try:
            from .db_client import processing_error
            from .client_diagnostics import failure_notice
            from .preferences import load_output_folder
            message+=failure_notice(Path(load_output_folder(folder.parent)),processing_error(exc,'PC 삭제 관리 연결'),'PC 삭제 관리 연결')
        except Exception:pass
        try:publish_signal(signal,token,'failed',message=message)
        except (OSError,ValueError):pass
        # Keep a failure signal for the index to read; it carries no file data.
        signal=None
        raise ValueError(message) from None
    finally:
        if viewer:viewer.close()
        if signal:
            try:signal.unlink(missing_ok=True)
            except OSError:pass


if __name__=='__main__':
    # ShellExecute does not inherit the source checkout's working directory.
    # This absolute entry point also exists in an installed Python package.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from segio_archive.__main__ import main
    raise SystemExit(main())
