"""Windows preparation failures must never change the completed backup."""
import errno
import json
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch

import test_date_incremental as fixtures
from segio_archive.backup_folders import BaselineValidationError
from segio_archive.client_diagnostics import write_failure
from segio_archive.date_incremental import DateBaseline
from segio_archive.db_client import processing_error


def denied(number):
    error=PermissionError(errno.EACCES,'PRIVATE_PATH_AND_TEXT')
    error.winerror=number
    return error


class IncrementalIOTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DateIncrementalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base=self.fixture.base
        self.old=self.fixture.old
        self.before=self.fixture.snapshot()

    def incoming(self):
        (self.fixture.mail/'new.eml').write_bytes(b'Subject: New private-copy test\n\nnew message')
        return self.fixture.export(2,self.base)

    def test_locked_directory_rename_is_avoided_and_only_received_files_move(self):
        import os
        target=self.incoming()
        replace=os.replace
        directory_moves=[]
        def no_directory_move(source,destination):
            if Path(source)==target/'mail/raw':
                directory_moves.append((source,destination))
                raise denied(5)
            return replace(source,destination)
        with patch('segio_archive.file_ops.os.replace',side_effect=no_directory_move):
            self.fixture.prepare(target)
        self.assertEqual(directory_moves,[])
        self.assertEqual(self.before,self.fixture.snapshot())

    def test_sharing_lock_on_received_file_has_bounded_retry_and_then_succeeds(self):
        import os
        target=self.incoming()
        replace=os.replace
        attempts=[]
        def transient(source,destination):
            if Path(destination).parent==target/'.incoming-mail':
                attempts.append(1)
                if len(attempts)<3:raise denied(32)
            return replace(source,destination)
        with patch('segio_archive.file_ops.os.replace',side_effect=transient),patch('segio_archive.file_ops.time.sleep'):
            self.fixture.prepare(target)
        self.assertEqual(len(attempts),3)
        self.assertEqual(self.before,self.fixture.snapshot())

    def test_permanent_access_denial_stops_once_and_records_operation_without_paths(self):
        import os
        target=self.incoming()
        replace=os.replace
        attempts=[]
        def permanent(source,destination):
            if Path(destination).parent==target/'.incoming-mail':
                attempts.append(1)
                raise denied(5)
            return replace(source,destination)
        with patch('segio_archive.file_ops.os.replace',side_effect=permanent),patch('segio_archive.file_ops.time.sleep') as waiting:
            try:
                self.base.prepare(target,lambda:None,lambda *a,**k:None,lambda _:None)
            except PermissionError as exc:
                error=processing_error(exc,'기존 백업 반영 준비')
            else:
                self.fail('Permanent access denial must stop preparation')
        self.assertEqual(len(attempts),1)
        waiting.assert_not_called()
        path=write_failure(self.fixture.root/'diagnostics',error,'기존 백업 반영 준비')
        value=json.loads(path.read_bytes())
        self.assertEqual(value['error'],'FILE_ACCESS_DENIED')
        self.assertEqual(value['filesystem']['winerror'],5)
        self.assertEqual(value['merge'],{'operation':'stage_incoming_mail','file_kind':'mail_raw'})
        self.assertIn(value['processing']['site']['module'],('date_incremental','file_ops'))
        self.assertNotIn('PRIVATE',path.read_text())
        self.assertNotIn(str(self.old),path.read_text())
        self.assertEqual(self.before,self.fixture.snapshot())

    def test_readonly_raw_mail_uses_private_writable_copy_and_keeps_original_attributes(self):
        import os
        target=self.incoming()
        relative=self.base.sources[0]['path']
        source=self.old/relative
        mode=source.stat().st_mode
        source.chmod(mode&~(stat.S_IWUSR|stat.S_IWGRP|stat.S_IWOTH))
        link=os.link
        try:
            def no_readonly_link(old,new):
                self.assertNotEqual(Path(old),source,'Read-only source must not be hard-linked')
                return link(old,new)
            with patch('segio_archive.date_incremental.os.link',side_effect=no_readonly_link):
                self.fixture.prepare(target)
            self.assertFalse(source.stat().st_mode&stat.S_IWUSR)
            self.assertTrue((target/relative).stat().st_mode&stat.S_IWUSR)
            self.assertNotEqual(source.stat().st_ino,(target/relative).stat().st_ino)
            self.assertEqual(self.before,self.fixture.snapshot())
        finally:source.chmod(mode)

    def test_changed_mailbox_does_not_clone_or_unlink_the_previous_raw_inode(self):
        import os
        with (self.fixture.mail/'inbox.mbox').open('ab') as stream:
            stream.write(b'\nFrom new@example.test Tue Oct 20 13:00:00 2026\nSubject: Changed mailbox\n\nnew\n')
        target=self.fixture.export(2,self.base,changed=['inbox.mbox'])
        original=next(item for item in self.base.sources if item['name']=='inbox.mbox')
        source=self.old/original['path']
        link=os.link
        copy=self.base.copy
        def no_old_link(old,new):
            self.assertNotEqual(Path(old),source)
            return link(old,new)
        def no_old_copy(old,*args,**kwargs):
            self.assertNotEqual(Path(old),source)
            return copy(old,*args,**kwargs)
        with patch('segio_archive.date_incremental.os.link',side_effect=no_old_link),patch.object(self.base,'copy',side_effect=no_old_copy):
            self.fixture.prepare(target)
        self.assertEqual(self.before,self.fixture.snapshot())
        self.assertNotEqual((target/original['path']).read_bytes(),source.read_bytes())

    def test_copy_read_denial_identifies_raw_mail_and_preserves_previous_bytes(self):
        target=self.incoming()
        source=self.old/self.base.sources[0]['path']
        opened=Path.open
        attempts=[]
        def read_denied(path,*args,**kwargs):
            if path==source:
                attempts.append(1)
                raise denied(5)
            return opened(path,*args,**kwargs)
        with patch('segio_archive.date_incremental.os.link',side_effect=OSError(errno.EXDEV,'NO_LINKS')),patch.object(Path,'open',read_denied):
            with self.assertRaises(PermissionError) as caught:
                self.base.prepare(target,lambda:None,lambda *a,**k:None,lambda _:None)
        self.assertEqual(len(attempts),1)
        self.assertEqual(caught.exception.merge_diagnostic,{'operation':'read_previous','file_kind':'mail_raw'})
        self.assertEqual(self.before,self.fixture.snapshot())

    def test_security_block_on_raw_link_does_not_trigger_copy_fallback(self):
        import os
        target=self.incoming()
        source=self.old/self.base.sources[0]['path']
        link=os.link
        copy=self.base.copy
        copied=[]
        def blocked(old,new):
            if Path(old)==source:raise denied(225)
            return link(old,new)
        def track_copy(old,*args,**kwargs):
            copied.append(Path(old))
            return copy(old,*args,**kwargs)
        with patch('segio_archive.date_incremental.os.link',side_effect=blocked),patch.object(self.base,'copy',side_effect=track_copy):
            with self.assertRaises(PermissionError) as caught:
                self.base.prepare(target,lambda:None,lambda *a,**k:None,lambda _:None)
        self.assertNotIn(source,copied)
        self.assertIn('FILE_SECURITY_BLOCKED',str(processing_error(caught.exception,'기존 백업 반영 준비')))
        self.assertEqual(self.before,self.fixture.snapshot())

    def test_baseline_failures_identify_reason_and_do_not_rewrite_integrity_records(self):
        manifest_path=self.old/'manifest.json'
        original=manifest_path.read_bytes()
        for reason in ('account_mismatch','incomplete','time_unavailable','viewer_not_ready','portable_not_ready',
                       'catalog_changed','database_changed','mail_list_changed','mail_size_changed'):
            with self.subTest(reason=reason):
                value=json.loads(original)
                changed=None
                user='employee'
                if reason=='account_mismatch':user='PRIVATE_OTHER_ID'
                elif reason=='incomplete':value['local_processing']={'state':'pending'}
                elif reason=='time_unavailable':value['approval_checkpoint']='invalid'
                elif reason=='viewer_not_ready':value['viewer']='local-db/1'
                elif reason=='portable_not_ready':value['portable_mail']['version']='old'
                else:
                    name={'catalog_changed':'catalog.json','database_changed':'catalog.sqlite3',
                          'mail_list_changed':'mail/sources.json','mail_size_changed':self.base.sources[0]['path']}[reason]
                    changed=self.old/name
                    payload=changed.read_bytes()
                    changed.write_bytes(payload+b' ')
                manifest_path.write_text(json.dumps(value))
                try:
                    with self.assertRaises(BaselineValidationError) as caught:DateBaseline(self.old,user)
                    self.assertEqual(caught.exception.baseline_diagnostic,{'reason':reason})
                    path=write_failure(self.fixture.root/'diagnostics',caught.exception,'이전 백업 확인')
                    record=json.loads(path.read_bytes())
                    self.assertEqual(record['error'],'BASELINE_INVALID')
                    self.assertEqual(record['baseline'],{'reason':reason})
                    self.assertNotIn('PRIVATE',path.read_text())
                    self.assertEqual(json.loads(manifest_path.read_bytes()),value)
                finally:
                    if changed:changed.write_bytes(payload)
                    manifest_path.write_bytes(original)
        self.assertEqual(self.before,self.fixture.snapshot())

    @unittest.skipUnless(sys.platform=='win32','Native Windows directory handle required')
    def test_native_windows_directory_handle_does_not_block_file_only_preparation(self):
        import ctypes
        from ctypes import wintypes
        import os
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
                                    wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        target=self.incoming()
        raw=target/'mail/raw'
        handle=kernel.CreateFileW(str(raw),0x80000000,3,None,3,0x02000000,None)
        self.assertNotEqual(handle,wintypes.HANDLE(-1).value)
        try:
            probe=target/'.directory-rename-probe'
            try:os.replace(raw,probe)
            except OSError as exc:self.assertIn(exc.winerror,(5,32,33))
            else:
                os.replace(probe,raw)
                self.fail('Test directory handle must prevent directory rename')
            self.fixture.prepare(target)
            self.assertEqual(self.before,self.fixture.snapshot())
        finally:kernel.CloseHandle(handle)
