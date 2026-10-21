"""File locks must preserve verified originals and the first useful error."""
import errno
import hashlib
import io
import json
import os
from email.message import Message
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from segio_archive.archive import atomic_write, verify_archive
from segio_archive.client_diagnostics import write_failure
from segio_archive.db_client import DBCollector, processing_error
from segio_archive.file_ops import replace_file
from segio_archive.mail_import import build_local_catalog
from segio_archive.portable_mail import prepare_archive
from segio_archive.web import CollectionError, Config
import test_large_mail as large_fixture
import test_portable_mail as portable_fixture


def denied(winerror):
    error=PermissionError(errno.EACCES,'PRIVATE_EXCEPTION','C:/PRIVATE/SECRET')
    error.winerror=winerror
    return error


class WindowsFileRecoveryTests(unittest.TestCase):
    def test_transient_windows_sharing_locks_retry_then_replace_without_losing_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source,target=root/'pending',root/'index.html'
            source.write_bytes(b'new')
            target.write_bytes(b'old')
            original=os.replace
            calls=[]
            def sharing(a,b):
                calls.append(1)
                self.assertEqual(target.read_bytes(),b'old')
                if len(calls)<3:raise denied(32)
                return original(a,b)
            with patch('segio_archive.file_ops.os.replace',side_effect=sharing),patch('segio_archive.file_ops.time.sleep') as wait:
                replace_file(source,target)
            self.assertEqual(len(calls),3)
            self.assertEqual(wait.call_count,2)
            self.assertEqual(target.read_bytes(),b'new')

    def test_persistent_locks_stop_and_permanent_access_denial_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source,target=root/'pending',root/'index.html'
            source.write_bytes(b'new')
            target.write_bytes(b'old')
            for code,attempts in ((32,5),(33,5),(5,1)):
                with patch('segio_archive.file_ops.os.replace',side_effect=denied(code)) as move,patch('segio_archive.file_ops.time.sleep'):
                    with self.assertRaises(PermissionError) as caught:replace_file(source,target)
                self.assertEqual(move.call_count,attempts)
                self.assertEqual(caught.exception.local_operation,'replace')
                self.assertEqual(target.read_bytes(),b'old')
                self.assertEqual(source.read_bytes(),b'new')

    def test_atomic_metadata_failure_keeps_previous_bytes_and_safe_error_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            target=root/'manifest.json'
            target.write_bytes(b'original metadata')
            with patch('segio_archive.file_ops.os.replace',side_effect=denied(32)),patch('segio_archive.file_ops.time.sleep'):
                with self.assertRaises(PermissionError) as caught:atomic_write(target,b'new metadata')
            self.assertEqual(target.read_bytes(),b'original metadata')
            self.assertEqual(list(root.iterdir()),[target])
            error=processing_error(caught.exception,'Windows DB·메일 검색 색인 생성')
            record=write_failure(root,error,'Windows DB·메일 검색 색인 생성')
            value=json.loads(record.read_text())
            self.assertEqual(value['error'],'FILE_ACCESS_DENIED')
            self.assertEqual(value['filesystem'],{'exception_type':'PermissionError','errno':errno.EACCES,'winerror':32,'operation':'replace'})
            self.assertNotIn('PRIVATE',record.read_text())
            self.assertNotIn('SECRET',record.read_text())

    def download(self,root):
        payload=b'From synthetic@example.test\nSubject: Recover me\n\noriginal content\n'
        info={'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}
        contents={'index.html':b'<p>Import pending</p>','catalog.json':b'{"items":[]}',
                  'mail/raw/000000.bin':payload,
                  'mail/sources.json':json.dumps({'user':'employee','sources':[dict(info,name='inbox',path='mail/raw/000000.bin')]}).encode()}
        manifest={'format':'segio-db-html/1','user':'employee','state':'collected_with_warnings',
                  'account_deletion_verified':False,'counts':{'approval':0},'coverage':[],'warnings':[],
                  'live_export':{'job':'a'*32,'approvals':True,'mail':True},
                  'files':{name:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in contents.items()}}
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w',zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr('manifest.json',json.dumps(manifest))
            for name,data in contents.items():zipped.writestr(name,data)
        body=raw.getvalue()
        incoming=io.BytesIO(body)
        incoming.status=200
        incoming.headers=Message()
        incoming.headers['Content-Type']='application/zip'
        incoming.headers['Content-Length']=str(len(body))
        opener=Mock()
        opener.open.return_value=incoming
        client=DBCollector(Config('https://example.invalid/','employee',root/'output'))
        client.job='a'*32
        client.context,client.page=Mock(),Mock()
        client.user_agent='Synthetic Browser'
        record={'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
        return client,opener,record,payload

    def test_local_permission_error_preserves_verified_folder_without_moving_open_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            client,opener,record,payload=self.download(root)
            def fail(*args):raise denied(5)
            with patch('segio_archive.db_client.download_opener',return_value=opener),patch.object(client,'finalize_download',side_effect=fail),patch('segio_archive.db_client.publish_local') as moving:
                with self.assertRaisesRegex(CollectionError,'FILE_ACCESS_DENIED') as caught:client.receive(record)
                moving.assert_not_called()
            self.assertIn('LOCAL_INDEX_PENDING',str(caught.exception))
            self.assertNotIn('PRIVATE',str(caught.exception))
            saved=next((root/'output').glob('segio-employee*'))
            self.assertEqual((saved/'mail/raw/000000.bin').read_bytes(),payload)
            self.assertEqual(json.loads((saved/'manifest.json').read_text())['local_processing']['state'],'pending')
            self.assertFalse(list((root/'output').glob('.segio-download-*')))
            prepare_archive(saved)
            self.assertNotIn('local_processing',json.loads((saved/'manifest.json').read_text()))
            self.assertTrue(verify_archive(saved)['ok'])
            opener.open.assert_called_once()

    def test_initial_pending_marker_failure_still_preserves_a_recoverable_verified_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            client,opener,record,payload=self.download(root)
            with patch('segio_archive.db_client.download_opener',return_value=opener),patch('segio_archive.archive.atomic_write',side_effect=denied(5)):
                with self.assertRaisesRegex(CollectionError,'LOCAL_INDEX_PENDING'):client.receive(record)
            saved=next((root/'output').glob('segio-employee*'))
            self.assertEqual((saved/'mail/raw/000000.bin').read_bytes(),payload)
            self.assertTrue(verify_archive(saved)['ok'])
            prepare_archive(saved)
            self.assertTrue(verify_archive(saved)['ok'])
            self.assertTrue((saved/'catalog.sqlite3').is_file())

    def test_header_scan_failure_closes_all_mbox_handles_even_with_traceback_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            large_fixture.LargePersonalMailTests().personal_fixture(root)
            opened=[]
            original=Path.open
            def tracking(path,*args,**kwargs):
                stream=original(path,*args,**kwargs)
                if path.suffix=='.mbox':opened.append(stream)
                return stream
            with patch.object(Path,'open',tracking),patch('segio_archive.compact_mail.read_headers',side_effect=denied(5)):
                try:build_local_catalog(root,'employee')
                except PermissionError as error:retained=error
            self.assertIsNotNone(retained.__traceback__)
            self.assertGreaterEqual(len(opened),3)
            self.assertTrue(all(stream.closed for stream in opened))

    def test_interrupted_source_and_manifest_updates_recover_renamed_raw_mail_by_hash(self):
        for failed_name in ('sources.json','manifest.json'):
            with self.subTest(file=failed_name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                client,opener,record,payload=self.download(root)
                original=atomic_write
                def failed_write(path,data):
                    if path.name==failed_name and (path.parent/'raw/000000.mbox').exists():
                        raise denied(5)
                    if path.name==failed_name and (path.parent/'mail/raw/000000.mbox').exists():
                        raise denied(5)
                    return original(path,data)
                with patch('segio_archive.db_client.download_opener',return_value=opener),patch('segio_archive.archive.atomic_write',side_effect=failed_write):
                    with self.assertRaisesRegex(CollectionError,'LOCAL_INDEX_PENDING'):client.receive(record)
                saved=next((root/'output').glob('segio-employee*'))
                self.assertEqual((saved/'mail/raw/000000.mbox').read_bytes(),payload)
                prepare_archive(saved)
                self.assertTrue(verify_archive(saved)['ok'])
                self.assertEqual((saved/'mail/raw/000000.mbox').read_bytes(),payload)

    def test_render_error_releases_database_and_mail_handles_for_local_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            portable_fixture.PortableMailTests().fixture(root)
            with patch('segio_archive.portable_mail.render_record',side_effect=denied(5)):
                try:prepare_archive(root)
                except PermissionError as error:retained=error
            self.assertIsNotNone(retained.__traceback__)
            # Windows refuses these renames if traceback-retained cursors or
            # MBOX generators still own file handles.
            for path in (root/'catalog.sqlite3',root/'mail/raw/000000.mbox'):
                temporary=path.with_name(path.name+'.probe')
                path.rename(temporary)
                temporary.rename(path)
            prepare_archive(root)
            self.assertTrue(verify_archive(root)['ok'])

    def test_rename_recovery_rejects_changed_bytes_and_ambiguous_original_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            manifest=large_fixture.LargePersonalMailTests().personal_fixture(root)
            raw=root/'mail/raw/000000.mbox'
            original=raw.read_bytes()
            renamed=raw.with_suffix('.bin')
            raw.rename(renamed)
            renamed.write_bytes(b'x'*len(original))
            with self.assertRaisesRegex(CollectionError,'SHA-256'):build_local_catalog(root,'employee')
            self.assertEqual(renamed.read_bytes(),b'x'*len(original))
            renamed.write_bytes(original)
            raw.write_bytes(original)
            with self.assertRaisesRegex(CollectionError,'검증 정보'):build_local_catalog(root,'employee')
            self.assertEqual(renamed.read_bytes(),original)
            self.assertEqual(raw.read_bytes(),original)
            self.assertEqual(json.loads((root/'manifest.json').read_text()),manifest)

    @unittest.skipUnless(sys.platform=='win32','Native Windows sharing violation')
    def test_native_windows_sharing_lock_can_clear_during_bounded_retry(self):
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.CloseHandle.restype=wintypes.BOOL
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            target,source=root/'index.html',root/'index.html.tmp'
            target.write_bytes(b'old')
            source.write_bytes(b'new')
            handle=kernel.CreateFileW(str(target),0x80000000,0,None,3,0x80,None)
            self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
            release=threading.Timer(.2,lambda:kernel.CloseHandle(handle))
            release.start()
            try:replace_file(source,target)
            finally:release.join()
            self.assertEqual(target.read_bytes(),b'new')


if __name__=='__main__':unittest.main()
