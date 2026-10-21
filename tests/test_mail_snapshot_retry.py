"""Changing live mail must either stabilize intact or fail without publication."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import live_export_worker as worker


class MailSnapshotTests(unittest.TestCase):
    def fixture(self,root):
        mail=root/'employee'
        mail.mkdir()
        (mail/'a.mbox').write_bytes(b'From a@example.test\nSubject: A\n\noriginal\n')
        (mail/'b.mbox').write_bytes(b'From b@example.test\nSubject: B\n\noriginal\n')
        output=root/'output'
        output.mkdir()
        return mail,output,{'files':{},'counts':{},'coverage':[]}

    def assert_intact(self,mail,output,manifest):
        sources=worker.read_json(str(output/'mail/sources.json'))
        self.assertTrue(sources['raw_files_complete'])
        self.assertEqual({entry['name'] for entry in sources['sources']},{p.name for p in mail.iterdir()})
        for entry in sources['sources']:
            data=(output/entry['path']).read_bytes()
            self.assertEqual(data,(mail/entry['name']).read_bytes())
            self.assertEqual(manifest['files'][entry['path']],{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})

    def test_changed_file_is_retried_and_unchanged_large_files_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            mail,output,manifest=self.fixture(root)
            original=worker.copy_mail_file
            copied=[]
            def copying(path,*args):
                copied.append(Path(path).name)
                info=original(path,*args)
                if copied==['a.mbox']:
                    with (mail/'a.mbox').open('ab') as stream:stream.write(b'new incoming mail\n')
                return info
            with patch.object(worker,'copy_mail_file',side_effect=copying):
                worker.copy_mail(str(root/'{user}'),'employee',str(output),manifest,lambda:None)
            self.assertEqual(copied,['a.mbox','b.mbox','a.mbox'])
            self.assert_intact(mail,output,manifest)

    def test_new_deleted_and_renamed_files_are_reconciled_without_stale_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            mail,output,manifest=self.fixture(root)
            original=worker.inventory
            calls=0
            def listing(path):
                nonlocal calls
                calls+=1
                if calls==2:
                    (mail/'a.mbox').unlink()
                    (mail/'b.mbox').rename(mail/'renamed.mbox')
                    (mail/'new.mbox').write_bytes(b'new complete message\n')
                return original(path)
            with patch.object(worker,'inventory',side_effect=listing):
                worker.copy_mail(str(root/'{user}'),'employee',str(output),manifest,lambda:None)
            self.assert_intact(mail,output,manifest)
            self.assertEqual(len(list((output/'mail/raw').iterdir())),2)

    def test_continuously_changed_mail_stops_after_three_attempts_without_complete_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            mail,output,manifest=self.fixture(root)
            original=worker.copy_mail_file
            copies=[]
            def copying(path,*args):
                copies.append(Path(path).name)
                result=original(path,*args)
                if Path(path).name=='a.mbox':
                    with (mail/'a.mbox').open('ab') as stream:stream.write(b'changed again\n')
                return result
            with patch.object(worker,'copy_mail_file',side_effect=copying):
                with self.assertRaisesRegex(worker.SafeError,'mail_changed_retry'):
                    worker.copy_mail(str(root/'{user}'),'employee',str(output),manifest,lambda:None)
            self.assertEqual(copies.count('a.mbox'),3)
            self.assertEqual(copies.count('b.mbox'),1)
            self.assertFalse((output/'mail/sources.json').exists())
            self.assertEqual(manifest['files'],{})

    def test_cancellation_during_copy_and_symlink_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            mail,output,manifest=self.fixture(root)
            def cancelled():raise worker.SafeError('cancelled')
            with self.assertRaisesRegex(worker.SafeError,'cancelled'):
                worker.copy_mail(str(root/'{user}'),'employee',str(output),manifest,cancelled)
            if os.name!='nt':
                (mail/'linked').symlink_to(root/'PRIVATE')
                with self.assertRaisesRegex(worker.SafeError,'mail_unsafe_file'):
                    worker.copy_mail(str(root/'{user}'),'employee',str(output),manifest,lambda:None)
            self.assertFalse((output/'mail/sources.json').exists())

    def test_real_legacy_python_mail_retry_and_zip_progress(self):
        binary=os.environ.get('SEGIO_TEST_PYTHON26')
        if not binary:self.skipTest('Set SEGIO_TEST_PYTHON26 for the legacy runtime test')
        result=subprocess.run([binary,str(ROOT/'tests/legacy_mail_fixture.py'),str(ROOT/'tools')],
                              capture_output=True,text=True,timeout=30,env=dict(os.environ,LC_ALL='C'))
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('LEGACY_MAIL_OK',result.stdout)


if __name__=='__main__':unittest.main()
