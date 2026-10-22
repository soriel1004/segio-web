import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from segio_archive.archive import verify_archive
from segio_archive.db_client import unpack_verified
from segio_archive.mail_import import build_local_catalog
from segio_archive.progress import ProgressReporter, describe_progress
from segio_archive.web import CollectionError
from test_live_archive import create_fixture, worker


class ProgressTests(unittest.TestCase):
    def test_large_single_message_reports_scan_bytes_before_any_message_is_indexed(self):
        from segio_archive.compact_mail import mbox_spans
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'large.mbox'
            with path.open('wb') as stream:
                stream.write(b'From fixture@example.test\nSubject: One large message\n\n')
                for _ in range(10):stream.write(b'x'*1024*1024)
            values=[]
            spans=mbox_spans(path,progress=values.append)
            try:
                next(spans)
                self.assertGreaterEqual(len(values),10)
                self.assertEqual(values[-1],path.stat().st_size)
                self.assertEqual(values,sorted(values))
            finally:spans.close()
    def test_byte_percent_unknown_length_retry_and_mail_count_are_distinguished(self):
        values=[]
        report=ProgressReporter(values.append)
        report('mail', 25*1024**3, 50*1024**3, attempt=2)
        label,percent=describe_progress(values[-1])
        self.assertEqual(percent,50)
        self.assertIn('25.00 GiB / 50.00 GiB',label)
        self.assertIn('재시도 1/2',label)
        report('packing',5*1024**3)
        label,percent=describe_progress(values[-1])
        self.assertIsNone(percent)
        self.assertIn('5.00 GiB',label)
        report('render',1234,2468,unit='messages')
        self.assertEqual(describe_progress(values[-1])[1],50)
        self.assertIn('1,234 / 2,468건',describe_progress(values[-1])[0])
        report('database')
        self.assertIsNone(describe_progress(values[-1])[1])

    def test_progress_throttles_per_chunk_updates_but_emits_stage_changes_and_completion(self):
        events=[]
        report=ProgressReporter(events.append)
        with patch('segio_archive.progress.time.monotonic',return_value=10):
            for number in range(1000):report('download',number,1000)
            report('download',1000,1000)
            report('verify',0,1000)
        self.assertEqual(len(events),3)
        self.assertEqual(events[1]['completed'],1000)
        report('mail',-1,'PRIVATE',unit='PRIVATE',attempt=None)
        self.assertIsNone(events[-1]['completed'])
        self.assertIsNone(events[-1]['total'])
        self.assertNotIn('PRIVATE',json.dumps(events))

    def test_real_worker_and_local_pipeline_report_measured_progress_with_intact_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            job=root/'jobs'/('a'*32)
            job.mkdir()
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':1})
            statuses=[]
            original=worker.write_json
            def recording(path,value):
                if Path(path).name=='status.json':statuses.append(value)
                return original(path,value)
            with patch.object(worker,'write_json',side_effect=recording):
                worker.run_job(str(root/'config.json'),'a'*32)
            self.assertEqual(statuses[-1]['state'],'ready',statuses[-1])
            for stage in ('mail','packing','hashing'):
                measured=[value['progress'] for value in statuses if value.get('phase')==stage and 'progress' in value]
                self.assertTrue(measured,stage)
                self.assertTrue(all(type(value['completed_bytes']) is int for value in measured))
            events=[]
            report=ProgressReporter(events.append)
            output=root/'result'
            output.mkdir()
            unpack_verified(job/'archive.zip',output,'employee',progress=report)
            build_local_catalog(output,'employee',progress=report)
            for stage in ('verify','index','render'):
                measured=[value for value in events if value['stage']==stage]
                self.assertTrue(measured,stage)
                self.assertEqual(measured[-1]['completed'],measured[-1]['total'])
            self.assertEqual(measured[-1]['total'],3)
            self.assertTrue(verify_archive(output)['ok'])

    def test_zip_compression_can_be_cancelled_inside_a_file_and_does_not_publish_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            job=root/'jobs'/('a'*32)
            job.mkdir()
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':1})
            original=worker.PackingProgress.write
            def cancel_on_write(instance,data):
                (job/'cancel').touch()
                return original(instance,data)
            with patch.object(worker.PackingProgress,'write',cancel_on_write):
                worker.run_job(str(root/'config.json'),'a'*32)
            status=worker.read_json(str(job/'status.json'))
            self.assertEqual(status['state'],'failed')
            self.assertEqual(status['phase'],'cancelled')
            self.assertFalse((job/'archive.zip').exists())


@unittest.skipUnless(os.environ.get('DISPLAY') or sys.platform=='win32', 'A GUI display is required')
class ProgressWindowTests(unittest.TestCase):
    def test_raw_preserved_mail_warnings_are_visible_at_backup_completion(self):
        from segio_archive.gui import App
        app=App()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                (root/'manifest.json').write_text(json.dumps({'state':'collected_with_warnings','warnings':[],
                    'portable_mail':{'warning_count':3,'raw_only_count':1,'unavailable_attachment_count':2,'source_only_count':1}}))
                app.running=True
                app.begin_progress('메일 변환')
                with patch('segio_archive.gui.messagebox.showwarning') as warning,patch('segio_archive.gui.messagebox.showinfo') as success:
                    app.events.put(('done',root))
                    app.poll()
                warning.assert_called_once()
                success.assert_not_called()
                self.assertIn('3건',warning.call_args.args[1])
                self.assertIn('원본 EML',warning.call_args.args[1])
                self.assertIn('첨부 2개',warning.call_args.args[1])
                self.assertIn('자동 복원하지 않았습니다',warning.call_args.args[1])
                self.assertIn('별도 EML을 생성하지 않았습니다',warning.call_args.args[1])
                self.assertIn('확인 필요',app.status.get())
        finally:
            app.running=False
            app.close()
    def test_stop_requires_confirmation_and_marks_only_this_backup_for_discard(self):
        from segio_archive.gui import App
        from segio_archive.web import Config
        app=App()
        try:
            app.running=True
            app.active_config=Config('https://example.invalid','employee',Path('/unused'),cancel=app.cancel_event)
            with patch('segio_archive.gui.messagebox.askokcancel',return_value=False):app.stop()
            self.assertFalse(app.cancel_event.is_set())
            self.assertFalse(app.active_config.discard_cancelled)
            with patch('segio_archive.gui.messagebox.askokcancel',return_value=True) as dialog:app.stop()
            self.assertIn('지금까지 진행한 내역이 삭제됩니다',dialog.call_args.args[1])
            self.assertTrue(app.cancel_event.is_set())
            self.assertTrue(app.active_config.discard_cancelled)
            self.assertEqual(str(app.stop_button.cget('state')),'disabled')
            with patch('segio_archive.gui.messagebox.showinfo') as dialog:
                app.events.put(('cancelled',('백업을 중단했습니다. 삭제했습니다.',True)))
                app.poll()
            dialog.assert_called_once()
            self.assertFalse(app.running)
            self.assertNotEqual(float(app.progress.cget('value')),100)
        finally:
            app.running=False
            app.close()
    def test_worker_queue_updates_real_progressbar_and_completion_error_states(self):
        from segio_archive.gui import App
        app=App()
        try:
            app.running=True
            app.begin_progress('로그인')
            app.events.put(('progress',{'stage':'download','completed':25*1024**3,
                                       'total':50*1024**3,'unit':'bytes','attempt':None}))
            app.poll()
            app.update_idletasks()
            self.assertEqual(str(app.progress.cget('mode')),'determinate')
            self.assertEqual(float(app.progress.cget('value')),50)
            self.assertIn('25.00 GiB / 50.00 GiB',app.progress_text.get())
            app.events.put(('progress',{'stage':'database','completed':None,
                                       'total':None,'unit':'bytes','attempt':None}))
            app.poll()
            self.assertEqual(str(app.progress.cget('mode')),'indeterminate')
            with patch('segio_archive.gui.messagebox.showerror') as dialog:
                app.events.put(('error','서버 연결 실패'))
                app.poll()
                self.assertFalse(app.running)
                self.assertIn('오류',app.progress_text.get())
                self.assertNotIn('백업 완료',app.progress_text.get())
                self.assertNotEqual(float(app.progress.cget('value')),100)
                dialog.assert_called_once()
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                (root/'manifest.json').write_text(json.dumps({'state':'collected','warnings':[]}))
                app.running=True
                app.begin_progress('다음 백업')
                with patch('segio_archive.gui.messagebox.showinfo') as completed:
                    app.events.put(('done',root))
                    app.poll()
                self.assertEqual(completed.call_args.args[0],'백업 완료')
                self.assertIn('백업이 완료되었습니다',completed.call_args.args[1])
                self.assertEqual(float(app.progress.cget('value')),100)
                self.assertIn('백업 완료',app.progress_text.get())
                (root/'manifest.json').write_text(json.dumps({'state':'collected','warnings':[],
                    'server_cleanup':{'state':'pending','archive_deleted':False}}))
                app.running=True
                app.begin_progress('서버 정리 확인')
                with patch('segio_archive.gui.messagebox.showwarning') as warning:
                    app.events.put(('done',root))
                    app.poll()
                warning.assert_called_once()
                self.assertIn('삭제 미확인',app.status.get())
                self.assertIn('삭제 미확인',app.progress_text.get())
        finally:
            app.running=False
            app.close()


if __name__=='__main__':unittest.main()
