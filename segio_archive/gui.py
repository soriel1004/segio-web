from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .archive import verify_archive, STATES
from .browser import BrowserUnavailable
from .web import Cancelled, CollectionError, Config, DEFAULT_SERVER
from . import __version__
from .progress import ProgressReporter, describe_progress

DB_MODE = "개인 SEGIO 로그인으로 자료 받기"
APP_TITLE = "그룹웨어 문서 및 메일 백업 및 검색"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE + " · v" + __version__)
        self.geometry("890x910")
        self.minsize(800, 700)
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.local_conversion = False
        self.active_config = None
        self.last_archive = None
        self.viewer = None
        self.server = tk.StringVar(value=DEFAULT_SERVER)
        self.user = tk.StringVar()
        self.password = tk.StringVar()
        self.folder = tk.StringVar(value=str(Path.home() / "Documents" / "SEGIO보관함"))
        self.approvals = tk.BooleanVar(value=True)
        self.mail = tk.BooleanVar(value=True)
        self.pdf = tk.BooleanVar(value=False)
        self.method = tk.StringVar(value=DB_MODE)
        self.http = tk.BooleanVar(value=True)
        self.manual = tk.BooleanVar(value=False)
        self.backup_mode = tk.StringVar(value='full')
        self.baseline_folder = tk.StringVar()
        self.backup_hint = tk.StringVar(value='전체 백업은 Windows 저장 폴더 아래 새 폴더에 저장합니다.')
        self.status = tk.StringVar(value="서버에 연결하기 전입니다.")
        self.progress_text = tk.StringVar(value='백업을 시작하면 단계별 처리량을 표시합니다.')
        self.progress_detail = ''
        self.started_at = None
        self.elapsed_seconds = 0
        self.build()
        self.after(100, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.close)
        from .window_control import minimize_console
        self.after_idle(minimize_console)

    def build(self):
        panel = ttk.Frame(self, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text=APP_TITLE, font=("Malgun Gothic", 18, "bold")).pack(anchor="w")
        ttk.Label(panel, text="서버의 원본을 보존하며 Windows에 백업합니다. 계정·문서·메일을 삭제하지 않습니다.", wraplength=770).pack(anchor="w", pady=(6, 10))
        ttk.Label(panel, text="그룹웨어 ID·암호로 로그인해 본인의 결재 문서와 메일을 내려받습니다.", wraplength=800).pack(anchor='w', pady=(0, 6))
        self.forms = ttk.Frame(panel)
        self.forms.pack(fill='x')
        form = ttk.Frame(self.forms)
        self.web_form = form
        form.pack(fill="x")
        for i, (label, variable, show) in enumerate([
            ("서버 주소", self.server, ""), ("그룹웨어 ID", self.user, ""),
            ("그룹웨어 암호", self.password, "•"),
        ]):
            ttk.Label(form, text=label, width=18).grid(row=i, column=0, sticky="w", pady=6)
            ttk.Entry(form, textvariable=variable, show=show).grid(row=i, column=1, sticky="ew", pady=6)
        form.columnconfigure(1, weight=1)
        folder_row = ttk.Frame(panel)
        folder_row.pack(fill='x',pady=5)
        ttk.Label(folder_row,text='Windows 저장 폴더',width=20).pack(side='left')
        ttk.Entry(folder_row,textvariable=self.folder).pack(side='left',fill='x',expand=True)
        ttk.Button(folder_row,text='선택',command=self.choose_folder).pack(side='left',padx=(8,0))
        mode_row=ttk.Frame(panel)
        mode_row.pack(fill='x',pady=5)
        ttk.Label(mode_row,text='백업 범위',width=20).pack(side='left')
        ttk.Radiobutton(mode_row,text='처음부터 전체 백업',variable=self.backup_mode,value='full',command=self.backup_mode_changed).pack(side='left')
        ttk.Radiobutton(mode_row,text='이전 백업 이후 추가 · 기존 폴더 반영',variable=self.backup_mode,value='incremental',command=self.backup_mode_changed).pack(side='left',padx=12)
        baseline_row=ttk.Frame(panel)
        baseline_row.pack(fill='x',pady=(0,5))
        ttk.Label(baseline_row,text='반영할 기존 백업',width=20).pack(side='left')
        ttk.Entry(baseline_row,textvariable=self.baseline_folder,state='readonly').pack(side='left',fill='x',expand=True)
        self.baseline_button=ttk.Button(baseline_row,text='선택',command=self.choose_baseline,state='disabled')
        self.baseline_button.pack(side='left',padx=(8,0))
        ttk.Label(panel,textvariable=self.backup_hint,wraplength=780).pack(fill='x',pady=(0,5))
        choices = ttk.Frame(panel)
        choices.pack(fill="x")
        self.collection_choices = []
        for label, var in [("결재 문서", self.approvals), ("메일", self.mail), ("결재 PDF 생성", self.pdf)]:
            widget = ttk.Checkbutton(choices, text=label, variable=var, state="disabled" if var is self.pdf else "normal")
            widget.pack(side="left", padx=(0, 18))
            self.collection_choices.append(widget)
        self.transport_options = ttk.Frame(panel)
        self.transport_options.pack(fill='x',pady=(8,0))
        self.web_options = ttk.Frame(self.transport_options)
        self.web_options.pack(fill="x")
        ttk.Checkbutton(self.web_options, text="암호화되지 않은 HTTP 연결 허용", variable=self.http).pack(anchor="w")
        ttk.Checkbutton(self.web_options, text="열린 브라우저에서 직접 로그인", variable=self.manual).pack(anchor="w")
        ttk.Label(panel, text="암호는 저장하지 않습니다. 50GB 메일함은 임시 ZIP·원본·열람용 첨부 공간을 고려해 여유 있는 NTFS 또는 exFAT 드라이브를 선택하세요.", wraplength=770, foreground="#76551b").pack(anchor="w", pady=8)
        actions = ttk.Frame(panel)
        actions.pack(fill="x", pady=5)
        self.start_button = ttk.Button(actions, text="로그인하고 수집 시작", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="중단", state="disabled", command=self.stop)
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="보관함 열기", command=self.open_archive).pack(side="left", padx=8)
        ttk.Button(actions, text="파일 무결성 검증", command=self.verify).pack(side="left")
        ttk.Button(actions, text="기존 백업 갱신", command=self.open_archive).pack(side="left", padx=8)
        ttk.Label(panel, textvariable=self.status, wraplength=770).pack(anchor="w", pady=(14, 6))
        self.progress = ttk.Progressbar(panel, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 8))
        ttk.Label(panel, textvariable=self.progress_text, wraplength=770).pack(anchor='w', pady=(0, 8))
        self.log_text = tk.Text(panel, height=7, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        ttk.Label(panel, text="수집 후 오프라인 열람·누락 대조를 수행하세요. 계정 삭제 영향 시험과 서버 전체 백업은 별도입니다.", wraplength=770).pack(anchor="w", pady=(10, 0))

    def choose_folder(self):
        value = filedialog.askdirectory(title="백업을 저장할 상위 폴더")
        if value:
            self.folder.set(value)

    def backup_mode_changed(self):
        self.baseline_button.config(state='normal' if self.backup_mode.get()=='incremental' else 'disabled')
        self.backup_hint.set('추가 백업은 선택한 기존 폴더에 반영합니다. 메일은 마지막 기준 시각 이후 파일만, 결재는 현재 상태를 갱신합니다.' if self.backup_mode.get()=='incremental' else '전체 백업은 Windows 저장 폴더 아래 새 폴더에 저장합니다.')

    def choose_baseline(self):
        if self.running:
            return
        value=filedialog.askdirectory(title='정상 완료한 이전 백업의 manifest.json이 있는 폴더',initialdir=self.folder.get())
        if value:
            self.baseline_folder.set(value)

    def start(self):
        if self.running:
            return
        self.local_conversion = False
        if not self.approvals.get() and not self.mail.get():
            return messagebox.showerror("선택 필요", "결재 문서 또는 메일을 선택하세요.")
        self.cancel_event = threading.Event()
        try:
            config = Config(server=self.server.get().strip(), username=self.user.get().strip(),
                            output=Path(self.folder.get()), approvals=self.approvals.get(),
                            mail=self.mail.get(), pdf=False, allow_http=self.http.get(),
                            manual_login=self.manual.get(), cancel=self.cancel_event)
            config.progress = lambda event:self.events.put(('progress',event))
            config.minimize_browser = True
            if self.backup_mode.get()=='incremental':
                if not self.baseline_folder.get():
                    raise ValueError('추가 자료를 반영할 기존 백업 폴더를 선택하세요.')
                config.incremental_base=Path(self.baseline_folder.get())
            password = self.password.get()
            if not password and not config.manual_login:
                raise ValueError("그룹웨어 암호를 입력하거나 브라우저 직접 로그인을 선택하세요.")
            config.validate()
        except Exception as exc:
            return messagebox.showerror("입력 확인", str(exc))
        self.password.set("")
        self.active_config=config
        self.running = True
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.begin_progress('그룹웨어 로그인 · 연결 준비 중')
        self.status.set("수집 중입니다. 프로그램이 연 브라우저 창을 닫지 마세요.")
        threading.Thread(target=self.worker, args=(config, password), daemon=True).start()

    def worker(self, config, password):
        collector = None
        try:
            from .db_client import DBCollector
            collector = DBCollector(config, lambda s:self.events.put(('log',s)))
            path = collector.run(password)
            if config.discard_cancelled and config.cancel.is_set() and not collector.commit_finished:
                collector.discard_local_on_cancel()
                raise Cancelled('백업을 중단했습니다. 이번 작업의 파일·폴더 정리 결과를 프로그램 로그에서 확인하세요.')
            self.events.put(("done", path))
        except Cancelled as exc:
            confirmed=bool(collector and collector.local_discard_confirmed and
                           (collector.cleanup_confirmed or (not collector.job and not collector.start_requested)))
            self.events.put(('cancelled',(str(exc),confirmed)))
        except (CollectionError, BrowserUnavailable) as exc:
            message = str(exc)
            if getattr(collector,'failure_path',None):
                message += '\n\n오류 기록: '+str(collector.failure_path)
            self.events.put(("error", message))
        except Exception as exc:
            self.events.put(("error", f"시작 실패 ({type(exc).__name__}). 저장 폴더 권한과 설치 상태를 확인하세요."))
        finally:
            password = ""

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'progress':
                    self.update_progress(value)
                elif event == "log":
                    self.log_text.config(state="normal")
                    self.log_text.insert("end", value + "\n")
                    self.log_text.see("end")
                    self.log_text.config(state="disabled")
                elif event in {"done", "error", "cancelled", "archive_ready", "archive_error"}:
                    self.running = False
                    self.active_config = None
                    self.progress.stop()
                    self.start_button.config(state="normal")
                    self.stop_button.config(state="disabled")
                    if event == 'cancelled':
                        message,confirmed=value
                        self.finish_progress('백업 중단 · 자료 삭제 완료' if confirmed else '백업 중단 · 삭제 확인 필요',False)
                        self.status.set(message)
                        dialog=messagebox.showinfo if confirmed else messagebox.showwarning
                        dialog('백업 중단',message)
                    elif event == 'archive_ready':
                        self.finish_progress('기존 백업 갱신 완료', True)
                        self.last_archive=value
                        self.status.set('메일 바로보기 준비 완료 · 이제 프로그램을 종료해도 index.html에서 열람할 수 있습니다.')
                        webbrowser.open((value/'index.html').as_uri())
                        manifest=json.loads((value/'manifest.json').read_text(encoding='utf-8'))
                        notice=self.mail_warning(value,manifest)
                        if notice:
                            self.status.set('기존 백업 갱신 완료 · 일부 메일 확인 필요')
                            messagebox.showwarning('백업 갱신 완료 · 메일 확인 필요',notice)
                    elif event == 'archive_error':
                        self.finish_progress(self.progress_detail + ' · 중단 또는 오류', False)
                        self.status.set(value)
                        messagebox.showerror('백업 갱신 확인',value)
                    elif event == "done":
                        self.last_archive = value
                        manifest = json.loads((value / "manifest.json").read_text(encoding="utf-8"))
                        self.finish_progress('백업 실패' if manifest['state']=='failed' else '백업 완료', manifest['state']!='failed')
                        self.status.set(f"{STATES.get(manifest['state'], manifest['state'])} · {value}")
                        notice=self.mail_warning(value,manifest)
                        if manifest["state"] == "failed":
                            detail = "\n".join(manifest["warnings"][1:][-3:]) or "수집 로그를 확인하세요."
                            messagebox.showerror("수집 실패", detail)
                        elif manifest.get('server_cleanup',{}).get('state')=='pending':
                            self.finish_progress('PC 백업 완료 · 서버 임시 ZIP 삭제 미확인', True)
                            self.status.set('PC 백업 완료 · 서버 임시 ZIP 삭제 미확인 · '+str(value))
                            messagebox.showwarning('서버 정리 확인','PC 백업은 저장됐지만 서버 임시 ZIP 삭제를 확인하지 못했습니다. 서버 정리 상태와 프로그램 로그를 확인하세요.\n저장 폴더: '+str(value)+ ('\n\n'+notice if notice else ''))
                        elif notice:
                            self.finish_progress('백업 완료 · 일부 메일 확인 필요',True)
                            self.status.set('백업 완료 · 일부 메일 확인 필요 · '+str(value))
                            messagebox.showwarning('백업 완료 · 메일 확인 필요',notice)
                        else:
                            messagebox.showinfo("백업 완료", "백업이 완료되었습니다.\n저장 폴더: "+str(value))
                    else:
                        self.finish_progress(self.progress_detail + ' · 중단 또는 오류', False)
                        self.status.set(value)
                        messagebox.showerror("수집 실패", value)
        except queue.Empty:
            pass
        self.refresh_progress_text()
        self.after(100, self.poll)

    def mail_warning(self,path,manifest):
        info=manifest.get('portable_mail',{})
        count=info.get('warning_count',0)
        if type(count) is not int or count<=0:return None
        raw=info.get('raw_only_count',0)
        raw=raw if type(raw) is int and 0<=raw<=count else 0
        unavailable=info.get('unavailable_attachment_count',0)
        unavailable=unavailable if type(unavailable) is int and unavailable>=0 else 0
        source_only=info.get('source_only_count',0)
        source_only=source_only if type(source_only) is int and source_only>=0 else 0
        return ('백업 목록을 생성했습니다. 메일 %d건에 변환·첨부 확인 사항이 있습니다.\n'%count+
                ('본문·첨부 자동 변환을 완료하지 못한 %d건은 원본 EML로 보존했습니다.\n'%raw if raw else '')+
                ('첨부 %d개는 확보 상태를 확인하지 못했습니다. 보안 기록·미확보 사유를 확인하세요. 자동 복원하지 않았습니다.\n'%unavailable if unavailable else '')+
                ('메일 %d건은 기존 원본 자료만 유지하며 별도 EML을 생성하지 않았습니다.\n'%source_only if source_only else '')+
                '보관함의 ‘확인 필요 메일 목록’에서 확인하세요.\n저장 폴더: '+str(path))

    def refresh_progress_text(self):
        if self.started_at is not None:
            self.elapsed_seconds = int(time.monotonic() - self.started_at)
            if not self.running:
                self.started_at = None
        if self.progress_detail:
            seconds = self.elapsed_seconds
            self.progress_text.set('%s · 경과 %02d:%02d:%02d' %
                                   (self.progress_detail, seconds//3600, seconds//60%60, seconds%60))

    def finish_progress(self, detail, success):
        self.progress.stop()
        if success:
            self.progress.config(mode='determinate', value=100)
        elif str(self.progress.cget('mode'))=='indeterminate':
            self.progress.config(mode='determinate', value=0)
        self.progress_detail = detail
        self.refresh_progress_text()

    def begin_progress(self, detail):
        self.started_at = time.monotonic()
        self.elapsed_seconds = 0
        self.progress_detail = detail
        self.progress_text.set(detail)
        self.progress.stop()
        self.progress.config(mode='indeterminate', value=0)
        self.progress.start()

    def update_progress(self, value):
        self.progress_detail, percent = describe_progress(value)
        mode = 'indeterminate' if percent is None else 'determinate'
        if str(self.progress.cget('mode')) != mode:
            self.progress.stop()
            self.progress.config(mode=mode, value=0)
            if percent is None:
                self.progress.start()
        if percent is not None:
            self.progress.config(value=percent)

    def stop(self):
        if not self.running:
            return
        if self.local_conversion:
            if messagebox.askokcancel('변환 중단','메일 변환을 중단하시겠습니까? 기존에 완료한 백업 원본은 보존합니다.') and self.running:
                self.cancel_event.set()
                self.stop_button.config(state='disabled')
                self.status.set('메일 변환을 중단합니다. 기존 백업 원본은 유지합니다.')
            return
        confirmed=messagebox.askokcancel('백업 중단 확인',
            '지금까지 진행한 내역이 삭제됩니다.\n이번 백업의 PC 파일과 서버 임시 작업 파일·폴더를 삭제하고 중단하시겠습니까?\n이전에 완료한 백업과 그룹웨어 원본은 유지합니다.')
        if not confirmed or not self.running:
            return
        if self.active_config:
            self.active_config.discard_cancelled=True
        self.cancel_event.set()
        self.stop_button.config(state='disabled')
        self.status.set('백업을 중단하고 이번 작업의 파일·폴더를 정리하는 중입니다. 삭제 확인까지 기다려 주세요.')

    def select_archive(self):
        value = filedialog.askdirectory(title="index.html과 manifest.json이 있는 보관 폴더",
                                        initialdir=str(self.last_archive or self.folder.get()))
        return Path(value) if value else None

    def open_archive(self):
        if self.running:
            return
        path = self.select_archive()
        if path:
            if (path / "index.html").is_file():
                from .viewer import Viewer
                try:
                    manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
                    if (manifest.get('viewer')=='sqlite-mail/1' or
                            manifest.get('local_processing',{}).get('state')=='pending'):
                        self.local_conversion=True
                        self.running=True
                        self.cancel_event=threading.Event()
                        self.start_button.config(state='disabled')
                        self.stop_button.config(state='normal')
                        self.begin_progress('기존 백업의 메일 본문·첨부 준비 중')
                        self.status.set('기존 백업의 메일 본문·첨부를 바로보기 파일로 변환합니다. 서버 접속 없이 진행합니다.')
                        threading.Thread(target=self.archive_worker,args=(path,),daemon=True).start()
                        return
                    self.status.set('보관함 목록·검색 화면을 준비합니다. 서버에서 자료를 다시 받지 않습니다.')
                    self.update_idletasks()
                    viewer = Viewer(path)
                    if self.viewer:
                        self.viewer.close()
                    self.viewer = viewer
                    webbrowser.open(self.viewer.url)
                    self.status.set('보관함을 열었습니다. 결재·메일 탭과 검색 버튼을 사용하세요.')
                except Exception as exc:
                    self.status.set('보관함을 열지 못했습니다.')
                    messagebox.showerror('보관함 확인', str(exc))
            else:
                messagebox.showerror("보관함 확인", "선택한 폴더에 index.html이 없습니다.")

    def archive_worker(self,path):
        from .portable_mail import prepare_archive
        from .web import Cancelled
        def checkpoint():
            if self.cancel_event.is_set():
                raise Cancelled('메일 변환을 중단했습니다. 기존 백업 갱신을 다시 누르면 완료한 메일을 재사용합니다.')
        try:
            report = ProgressReporter(lambda event:self.events.put(('progress',event)))
            prepare_archive(path,checkpoint,lambda message:self.events.put(('log',message)),report)
            self.events.put(('archive_ready',path))
        except Cancelled as exc:
            self.events.put(('archive_error',str(exc)))
        except Exception as exc:
            from .db_client import processing_error
            from .client_diagnostics import write_failure
            error = exc if isinstance(exc,CollectionError) else processing_error(exc,'Windows DB·메일 검색 색인 생성')
            message = str(error)+'\n저장된 원본은 유지됩니다. 원인을 해결한 뒤 ‘기존 백업 갱신’을 다시 실행하세요.'
            try:
                recorded = write_failure(path.parent,error,'Windows DB·메일 검색 색인 생성')
                message += '\n오류 기록: '+str(recorded)
            except OSError:
                pass
            self.events.put(('archive_error',message))

    def verify(self):
        path = self.select_archive()
        if not path:
            return
        # Only the Tk thread opens dialogs.
        try:
            result = verify_archive(path)
            detail = "\n".join(x["file"] for x in result["errors"][:10])
            messagebox.showinfo("파일 무결성 검사", f"검사 {result['checked']}개 / 오류 {len(result['errors'])}개\n{detail}\n파일 검사는 서버 자료 누락이나 계정 삭제 안전성을 검증하지 않습니다.")
        except Exception:
            messagebox.showerror("검증 실패", "manifest.json과 보관 파일을 확인하세요.")

    def close(self):
        if self.running:
            messagebox.showinfo("수집 중", "‘중단’을 누르고 현재 요청이 끝난 뒤 창을 닫아주세요.")
            return
        if self.viewer:
            self.viewer.close()
        self.destroy()


def main():
    App().mainloop()
