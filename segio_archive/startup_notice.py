"""A stdlib-only startup notice, usable before third-party packages install."""
import argparse
import os
from pathlib import Path
import re
import tempfile
import uuid

SIGNAL_ENV = 'SEGIO_STARTUP_SIGNAL'
TEXT = '실행중 잠시만 기다려주세요'


def signal_directory(value):
    if not value:
        return None
    path=Path(value)
    base=Path(os.environ.get('TEMP') or tempfile.gettempdir()).resolve()
    if (not re.fullmatch(r'segio-startup-[a-f0-9]{32}',path.name) or
            path.parent.resolve()!=base or path.is_symlink() or not path.is_dir()):
        return None
    return path


def mark_ready():
    value=os.environ.get(SIGNAL_ENV)
    if not value:
        return True
    try:
        path=signal_directory(value)
        if path is None:
            return True
        with (path/'ready').open('x',encoding='ascii') as output:
            output.write('ready')
        return True
    except FileExistsError:
        return True
    except OSError:
        return False


def cleanup_signal(path):
    try:
        if signal_directory(str(path)) is None:
            return
        for name in ('ready','failed'):
            (path/name).unlink(missing_ok=True)
        path.rmdir()  # Never recursively delete an unexpected file or folder.
    except OSError:
        pass


def show_notice(path):
    import tkinter as tk
    from tkinter import messagebox,ttk

    root=tk.Tk()
    root.title('SEGIO 실행 준비')
    root.resizable(False,False)
    width,height=370,120
    root.geometry('%dx%d+%d+%d'%(width,height,max(0,(root.winfo_screenwidth()-width)//2),max(0,(root.winfo_screenheight()-height)//2)))
    frame=ttk.Frame(root,padding=18)
    frame.pack(fill='both',expand=True)
    ttk.Label(frame,text=TEXT,font=('Malgun Gothic',12)).pack(pady=(2,14))
    bar=ttk.Progressbar(frame,mode='indeterminate')
    bar.pack(fill='x')
    bar.start()
    root.deiconify()
    root.lift()
    timer=None

    def close():
        if timer is not None:
            try:root.after_cancel(timer)
            except tk.TclError:pass
        bar.stop()
        root.destroy()

    def poll():
        nonlocal timer
        try:
            if (path/'ready').is_file():
                close()
                return
            if (path/'failed').is_file():
                messagebox.showerror('실행 준비 오류','프로그램 실행 준비 중 오류가 발생했습니다. 작업표시줄의 명령 창에서 오류 내용을 확인하세요.',parent=root)
                close()
                return
        except OSError:
            pass
        timer=root.after(100,poll)

    root.protocol('WM_DELETE_WINDOW',close)
    timer=root.after(100,poll)
    try:
        root.mainloop()
    finally:
        cleanup_signal(path)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--token',action='store_true')
    parser.add_argument('--watch')
    args=parser.parse_args()
    if args.token:
        print(uuid.uuid4().hex)
        return 0
    path=signal_directory(args.watch)
    if path is None:
        return 1
    show_notice(path)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
