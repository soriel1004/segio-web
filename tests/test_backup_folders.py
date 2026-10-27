import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from segio_archive.backup_folders import backup_candidates, MissingBackupFile
from segio_archive.client_diagnostics import failure_notice
from segio_archive.db_client import DBCollector
from segio_archive.web import CollectionError, Config

GUI=bool(os.environ.get('DISPLAY')) or sys.platform=='win32'


def completed(parent,name,user='employee',finished='2026-10-26T12:00:00Z',**fields):
    path=parent/name
    path.mkdir()
    value={'format':'segio-db-html/1','state':'collected','user':user,'finished_at':finished}
    value.update(fields)
    (path/'manifest.json').write_text(json.dumps(value),encoding='utf-8')
    return path


class BackupFolderTests(unittest.TestCase):
    def test_discovery_only_returns_matching_completed_immediate_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            old=completed(root,'segio-employee-2026-10-26')
            latest=completed(root,'segio-employee-2026-10-27',finished='2026-10-27T12:00:00Z')
            completed(root,'other-user',user='other')
            completed(root,'pending',local_processing={'state':'pending'})
            completed(root,'failed',state='failed')
            completed(root,'.segio-hidden')
            nested=root/'unrelated'
            nested.mkdir()
            completed(nested,'nested-backup')
            (root/'last-client-failure.json').write_text('not backup metadata')
            self.assertEqual(backup_candidates(root,'employee'),[latest,old])

    def test_diagnostics_stay_in_selected_output_despite_working_folder_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            selected=root/'Windows selected'
            working=root/'existing backups'
            client=DBCollector(Config('https://example.invalid/','employee',selected))
            client.working_output=working
            client.phase='이전 백업 확인'
            client.record_failure(MissingBackupFile(root/'PRIVATE_ACCOUNT','mail/raw/000000.bin'))
            self.assertEqual(client.failure_path,selected/'last-client-failure.json')
            self.assertFalse(working.exists())
            value=json.loads(client.failure_path.read_bytes())
            self.assertEqual(value['error'],'BASELINE_FILE_MISSING')
            self.assertEqual(value['baseline'],{'required_file':'mail_raw'})
            self.assertEqual(value['phase'],'이전 백업 확인')
            self.assertNotIn('PRIVATE_ACCOUNT',client.failure_path.read_text())

    def test_missing_backup_stops_before_browser_and_does_not_require_failure_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            storage=root/'Windows selected'
            storage.mkdir()
            (storage/'last-client-failure.json').write_text('old diagnostic, not a backup')
            client=DBCollector(Config('https://example.invalid/','employee',storage,incremental_base=storage))
            with patch('segio_archive.db_client.launch_browser') as browser,patch('playwright.sync_api.sync_playwright') as playwright:
                with self.assertRaisesRegex(MissingBackupFile,'manifest.json'):
                    client.run('PRIVATE_PASSWORD')
            browser.assert_not_called()
            playwright.assert_not_called()
            self.assertFalse(client.start_requested)
            self.assertIsNone(client.job)
            value=json.loads((storage/'last-client-failure.json').read_bytes())
            self.assertEqual(value['baseline'],{'required_file':'manifest'})

    def test_existing_backup_conversion_uses_selected_output_and_success_writes_no_failure(self):
        from segio_archive.gui import App
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            backup=root/'previous location'/'backup'
            selected=root/'Windows selected'
            app=SimpleNamespace(events=queue.Queue(),cancel_event=threading.Event())
            with patch('segio_archive.portable_mail.prepare_archive',side_effect=FileNotFoundError(2,'PRIVATE_PATH')):
                App.archive_worker(app,backup,selected)
            event,message=app.events.get_nowait()
            self.assertEqual(event,'archive_error')
            self.assertIn(str(selected/'last-client-failure.json'),message)
            self.assertTrue((selected/'last-client-failure.json').is_file())
            self.assertFalse((backup.parent/'last-client-failure.json').exists())
            self.assertNotIn('PRIVATE_PATH',(selected/'last-client-failure.json').read_text())
            (selected/'last-client-failure.json').unlink()
            with patch('segio_archive.portable_mail.prepare_archive'):
                App.archive_worker(app,backup,selected)
            self.assertEqual(app.events.get_nowait(),('archive_ready',backup))
            self.assertFalse((selected/'last-client-failure.json').exists())

    def test_failure_write_denied_explains_missing_record_without_fallback(self):
        with patch('segio_archive.client_diagnostics.write_failure',side_effect=PermissionError()):
            notice=failure_notice(Path('/unavailable'),CollectionError('error'),'시작')
        self.assertIn('오류 기록을 저장하지 못했습니다',notice)
        self.assertNotIn('오류 기록:',notice)

    @unittest.skipUnless(GUI,'Desktop display required')
    def test_gui_resolves_parent_folder_and_multiple_backups_require_a_selection(self):
        from segio_archive.gui import App
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'LOCALAPPDATA':tmp}):
            root=Path(tmp)/'backups'
            root.mkdir()
            first=completed(root,'segio-employee-2026-10-26')
            completed(root,'different-employee',user='other')
            app=App()
            try:
                app.user.set('employee')
                app.folder.set(str(root))
                with patch('segio_archive.gui.filedialog.askdirectory',return_value=str(root)):
                    app.choose_baseline()
                self.assertEqual(app.baseline_folder.get(),str(first))
                latest=completed(root,'segio-employee-2026-10-27',finished='2026-10-27T12:00:00Z')
                with patch('segio_archive.gui.filedialog.askdirectory',return_value=str(root)),patch.object(app,'choose_backup_candidate',return_value=latest) as choose:
                    app.choose_baseline()
                choose.assert_called_once_with([latest,first])
                self.assertEqual(app.baseline_folder.get(),str(latest))
                with patch('segio_archive.gui.filedialog.askdirectory',return_value=str(root)),patch.object(app,'choose_backup_candidate',return_value=None):
                    app.choose_baseline()
                self.assertEqual(app.baseline_folder.get(),str(latest))
                empty=root/'empty'
                empty.mkdir()
                with patch('segio_archive.gui.filedialog.askdirectory',return_value=str(empty)),patch('segio_archive.gui.messagebox.showerror') as error:
                    app.choose_baseline()
                self.assertIn('manifest.json',error.call_args.args[1])
                self.assertTrue((root/'last-client-failure.json').is_file())
                self.assertFalse((empty/'last-client-failure.json').exists())
            finally:app.close()

    @unittest.skipUnless(GUI,'Desktop display required')
    def test_real_backup_choice_dialog_confirms_specific_folder_and_can_be_cancelled(self):
        import tkinter as tk
        from tkinter import ttk
        from segio_archive.gui import App
        app=App()
        try:
            choices=[Path('latest'),Path('previous')]
            def click(confirm):
                dialog=next(child for child in app.winfo_children() if isinstance(child,tk.Toplevel))
                panel=dialog.winfo_children()[0]
                listing=next(child for child in panel.winfo_children() if isinstance(child,tk.Listbox))
                listing.selection_clear(0,'end')
                listing.selection_set(1)
                buttons=panel.winfo_children()[-1]
                next(button for button in buttons.winfo_children() if isinstance(button,ttk.Button) and button.cget('text')==('선택' if confirm else '취소')).invoke()
            app.after(100,lambda:click(True))
            self.assertEqual(app.choose_backup_candidate(choices),choices[1])
            app.after(100,lambda:click(False))
            self.assertIsNone(app.choose_backup_candidate(choices))
        finally:app.close()
