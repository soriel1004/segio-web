from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="SEGIO 결재 문서·메일 오프라인 보관")
    sub = parser.add_subparsers(dest="command")
    native=sub.add_parser('manage-url',help=argparse.SUPPRESS)
    native.add_argument('url')
    collect = sub.add_parser("collect", help="서버에서 문서와 메일 수집")
    collect.add_argument("--server", required=True)
    collect.add_argument("--user", required=True)
    collect.add_argument("--output", type=Path, default=Path("archives"))
    collect.add_argument("--allow-http", action="store_true")
    collect.add_argument("--no-mail", action="store_true")
    collect.add_argument("--no-approvals", action="store_true")
    collect.add_argument("--no-pdf", action="store_true")
    collect.add_argument("--headless", action="store_true")
    collect.add_argument("--manual-login", action="store_true")
    collect.add_argument("--max-pages", type=int, default=300)
    db = sub.add_parser("collect-db", help="직원 로그인 후 현재 개인 DB 자료·서버 MBOX 원본 가져오기")
    db.add_argument("--server", required=True)
    db.add_argument("--user", required=True)
    db.add_argument("--output", type=Path, default=Path("archives"))
    db.add_argument("--allow-http", action="store_true")
    db.add_argument("--headless", action="store_true")
    db.add_argument("--manual-login", action="store_true")
    db.add_argument("--no-mail", action="store_true")
    db.add_argument("--no-approvals", action="store_true")
    ssh = sub.add_parser('collect-ssh',help='관리자 SSH 접속으로 직원 DB·MBOX 직접 백업')
    ssh.add_argument('--host',default='192.168.0.100')
    ssh.add_argument('--port',type=int,default=22)
    ssh.add_argument('--ssh-user',default='root')
    ssh.add_argument('--user',required=True,help='백업할 직원의 SEGIO ID')
    ssh.add_argument('--key-file',default='')
    ssh.add_argument('--host-key',help='관리자가 확인한 SHA256:... SSH 서버 지문')
    ssh.add_argument('--output',type=Path,default=Path('archives'))
    ssh.add_argument('--db-config',default='/home/segio/cell000/etc/db.conf')
    ssh.add_argument('--cell',default='/home/segio/cell000')
    ssh.add_argument('--mail-template',default='/home/segio/cell000/home/{initial}/{user}/mbox')
    ssh.add_argument('--zip',action='store_true')
    ssh.add_argument('--fresh',action='store_true',help='이전 임시 전송 대신 새 추출 시작')
    ssh.add_argument('--no-mail',action='store_true')
    ssh.add_argument('--no-approvals',action='store_true')
    verify = sub.add_parser("verify", help="수집 파일 SHA-256 검사")
    verify.add_argument("folder", type=Path)
    view = sub.add_parser("view", help="서버 연결 없이 로컬 보관함 열기")
    view.add_argument("folder", type=Path)
    prepare=sub.add_parser('prepare-offline',help='기존 SSH 백업의 메일 본문·첨부를 index.html 바로보기로 변환')
    prepare.add_argument('folder',type=Path)
    args = parser.parse_args()
    if args.command=='manage-url':
        from .native_archive import run_url
        try:
            run_url(args.url)
            return 0
        except Exception as exc:
            import tkinter as tk
            from tkinter import messagebox
            root=tk.Tk();root.withdraw()
            try:messagebox.showerror('PC 백업 관리 연결',str(exc) if isinstance(exc,ValueError) else '백업 폴더의 완료 상태와 접근 권한을 확인하세요.')
            finally:root.destroy()
            return 2
    if args.command=='prepare-offline':
        from .portable_mail import prepare_archive
        prepare_archive(args.folder,log=print)
        print('변환 완료: '+str(args.folder/'index.html'))
        return 0
    if args.command == 'collect-ssh':
        from .ssh_client import SSHCollector,SSHConfig
        from .web import CollectionError
        import hmac
        def confirm(host,algorithm,fingerprint):
            if args.host_key:
                return hmac.compare_digest(args.host_key,fingerprint)
            print(f'SSH 서버 {host} · {algorithm} · {fingerprint}')
            return input('관리자에게 확인한 지문과 일치하면 yes 입력: ').strip()=='yes'
        try:
            config=SSHConfig(host=args.host,port=args.port,ssh_user=args.ssh_user,username=args.user,
                output=args.output,key_file=args.key_file,db_config=args.db_config,cell=args.cell,
                mail_template=args.mail_template,approvals=not args.no_approvals,mail=not args.no_mail,
                resume=not args.fresh,make_zip=args.zip)
            config.validate()
            password=getpass.getpass('서버 SSH 암호 / 개인키 암호 (저장하지 않음): ')
            SSHCollector(config,print,confirm).run(password)
            return 2  # Collected with comparison/account-deletion warnings.
        except KeyboardInterrupt:
            return 130
        except CollectionError as exc:
            print(str(exc),file=sys.stderr)
            return 2
    if args.command == "view":
        import webbrowser
        import threading
        from .viewer import Viewer
        viewer = Viewer(args.folder)
        webbrowser.open(viewer.url)
        print("보관함을 열었습니다. 종료하려면 Ctrl+C를 누르세요.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            viewer.close()
        return 0
    if args.command == "verify":
        from .archive import verify_archive
        result = verify_archive(args.folder)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    if args.command in {"collect", "collect-db"}:
        from .web import Collector, CollectionError, Config
        from .browser import BrowserUnavailable
        try:
            config = Config(server=args.server, username=args.user, output=args.output,
                            allow_http=args.allow_http, approvals=not getattr(args, 'no_approvals', False),
                            mail=not args.no_mail,
                            pdf=False if args.command=='collect-db' else not args.no_pdf,
                            headless=args.headless, manual_login=args.manual_login,
                            max_pages=getattr(args, 'max_pages', 300))
            if not config.approvals and not config.mail:
                parser.error("결재 또는 메일 중 하나를 선택하세요.")
            password = "" if args.manual_login else getpass.getpass("암호 (저장하지 않음): ")
            if args.command == 'collect-db':
                from .db_client import DBCollector
                collector_class = DBCollector
            else:
                collector_class = Collector
            path = collector_class(config, print).run(password)
            password = ""
            status = json.loads((path / "manifest.json").read_text(encoding="utf-8"))["state"]
            return 0 if status == "collected" else 2
        except KeyboardInterrupt:
            return 130
        except (CollectionError, BrowserUnavailable) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"시작 실패: {type(exc).__name__}", file=sys.stderr)
            return 2
    from .gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
