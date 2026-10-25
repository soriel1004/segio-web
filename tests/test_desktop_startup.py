from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from segio_archive import preferences,startup_notice
from segio_archive.db_client import backup_date,reserve_personal_folder

ROOT=Path(__file__).resolve().parents[1]
GUI=bool(os.environ.get('DISPLAY')) or sys.platform=='win32'


class FolderPreferenceTests(unittest.TestCase):
    def test_first_run_default_and_invalid_settings_keep_original_default(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'LOCALAPPDATA':tmp}):
            default=Path(tmp)/'Documents'/'SEGIO보관함'
            self.assertEqual(preferences.load_output_folder(default),str(default))
            path=preferences.settings_path()
            path.parent.mkdir()
            for body in (b'not JSON',b'[]',b'x'*65537,json.dumps({'protocol':preferences.PROTOCOL,'output_folder':''}).encode()):
                path.write_bytes(body)
                self.assertEqual(preferences.load_output_folder(default),str(default))

    def test_changed_folder_persists_only_folder_and_failed_write_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'LOCALAPPDATA':tmp}):
            target=Path(tmp)/'백업 보관'/'new folder'
            self.assertTrue(preferences.save_output_folder(str(target)))
            self.assertFalse(target.exists())
            self.assertEqual(preferences.load_output_folder('/different-default'),str(target))
            data=json.loads(preferences.settings_path().read_bytes())
            self.assertEqual(set(data),{'protocol','output_folder'})
            with patch.object(preferences,'atomic_write',side_effect=PermissionError('locked')):
                self.assertFalse(preferences.save_output_folder(str(Path(tmp)/'another')))
            self.assertEqual(preferences.load_output_folder('/different-default'),str(target))

    @unittest.skipUnless(GUI,'Desktop display required')
    def test_folder_picker_and_manual_edit_survive_app_restart(self):
        from segio_archive.gui import App
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'LOCALAPPDATA':tmp}):
            app=App()
            try:
                self.assertEqual(app.folder.get(),str(Path.home()/'Documents'/'SEGIO보관함'))
                with patch('segio_archive.gui.filedialog.askdirectory',return_value=str(Path(tmp)/'chosen')):
                    app.choose_folder()
                app.remember_folder()
            finally:app.close()
            app=App()
            try:
                self.assertEqual(app.folder.get(),str(Path(tmp)/'chosen'))
                app.folder.set(str(Path(tmp)/'typed'))
            finally:app.close()  # Flush a manual edit even before the debounce.
            app=App()
            try:
                self.assertEqual(app.folder.get(),str(Path(tmp)/'typed'))
                self.assertEqual(app.user.get(),'')
                self.assertEqual(app.password.get(),'')
            finally:app.close()


class DatedFolderTests(unittest.TestCase):
    def test_korean_calendar_date_and_repeated_full_backups_preserve_previous_files(self):
        self.assertEqual(backup_date(datetime(2026,10,25,18,tzinfo=timezone.utc)),'2026-10-26')
        self.assertEqual(backup_date(datetime(2026,10,25,14,tzinfo=timezone.utc)),'2026-10-25')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            previous=root/'segio-employee'
            previous.mkdir()
            (previous/'original').write_bytes(b'PREVIOUS_BACKUP')
            first=reserve_personal_folder(root,'employee','2026-10-26')
            (first/'original').write_bytes(b'FIRST_DATED_BACKUP')
            second=reserve_personal_folder(root,'employee','2026-10-26')
            third=reserve_personal_folder(root,'employee','2026-10-27')
            self.assertEqual(first.name,'segio-employee-2026-10-26')
            self.assertEqual(second.name,'segio-employee-2026-10-26-2')
            self.assertEqual(third.name,'segio-employee-2026-10-27')
            self.assertEqual((first/'original').read_bytes(),b'FIRST_DATED_BACKUP')
            self.assertEqual((previous/'original').read_bytes(),b'PREVIOUS_BACKUP')

    def test_conflicting_zip_and_simultaneous_runs_use_distinct_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            old=root/'segio-employee-2026-10-26.zip'
            old.write_bytes(b'OLD_ZIP')
            with ThreadPoolExecutor(max_workers=2) as executor:
                results=list(executor.map(lambda _:reserve_personal_folder(root,'employee','2026-10-26'),range(2)))
            self.assertEqual({path.name for path in results},{'segio-employee-2026-10-26-2','segio-employee-2026-10-26-3'})
            self.assertEqual(old.read_bytes(),b'OLD_ZIP')


class StartupNoticeTests(unittest.TestCase):
    def signal(self,root):
        path=Path(root)/('segio-startup-'+uuid.uuid4().hex)
        path.mkdir()
        return path

    def test_token_generator_runs_without_installed_third_party_dependencies(self):
        result=subprocess.run([sys.executable,'-S',str(ROOT/'segio_archive/startup_notice.py'),'--token'],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertRegex(result.stdout.strip(),r'^[a-f0-9]{32}$')

    def test_signal_is_isolated_and_cleanup_does_not_delete_other_files(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TEMP':tmp}):
            first,second=self.signal(tmp),self.signal(tmp)
            with patch.dict(os.environ,{startup_notice.SIGNAL_ENV:str(first)}):
                self.assertTrue(startup_notice.mark_ready())
            self.assertTrue((first/'ready').exists())
            self.assertFalse((second/'ready').exists())
            (first/'unexpected').write_bytes(b'KEEP')
            startup_notice.cleanup_signal(first)
            self.assertEqual((first/'unexpected').read_bytes(),b'KEEP')
            outside=Path(tmp)/'backup'
            outside.mkdir()
            (outside/'ready').write_bytes(b'KEEP')
            startup_notice.cleanup_signal(outside)
            self.assertEqual((outside/'ready').read_bytes(),b'KEEP')

    @unittest.skipUnless(GUI,'Desktop display required')
    def test_startup_notice_waits_then_exits_when_backup_window_is_visible(self):
        from segio_archive.gui import App
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TEMP':tmp,'LOCALAPPDATA':tmp}):
            signal=self.signal(tmp)
            with patch.dict(os.environ,{startup_notice.SIGNAL_ENV:str(signal)}):
                child=subprocess.Popen([sys.executable,'-S',str(ROOT/'segio_archive/startup_notice.py'),'--watch',str(signal)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                app=None
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):child.wait(timeout=.3)
                    self.assertFalse((signal/'ready').exists())
                    app=App()
                    deadline=time.monotonic()+5
                    while child.poll() is None and time.monotonic()<deadline:
                        app.update()
                        time.sleep(.02)
                    self.assertTrue(app.winfo_viewable())
                    self.assertEqual(child.wait(timeout=2),0)
                    self.assertFalse(signal.exists())
                finally:
                    if app:app.close()
                    if child.poll() is None:child.terminate()
                    child.communicate(timeout=5)

    @unittest.skipUnless(GUI,'Desktop display required')
    def test_setup_failure_closes_notice_and_shows_error(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TEMP':tmp}):
            signal=self.signal(tmp)
            timer=threading.Timer(.15,lambda:(signal/'failed').write_text('dependencies_failed'))
            timer.start()
            try:
                with patch('tkinter.messagebox.showerror') as error:
                    startup_notice.show_notice(signal)
                error.assert_called_once()
                self.assertFalse(signal.exists())
            finally:timer.join()
