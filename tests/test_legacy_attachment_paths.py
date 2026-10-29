"""Long names from completed mail backups must not break incremental merging."""
from email.message import EmailMessage
import errno
import json
import os
import unittest
from unittest.mock import patch

import test_date_incremental as fixtures
import test_windows_file_recovery as downloads
from test_db_client import make_archive
from segio_archive import portable_mail
from segio_archive.archive import safe_name, legacy_mail_attachment_name, verify_archive
from segio_archive.attachment_safety import attachment_name, legacy_attachment_alias
from segio_archive.client_diagnostics import write_failure
from segio_archive.compact_mail import digest_file, mail_record, search
from segio_archive.date_incremental import DateBaseline
from segio_archive.db_client import DBCollector, existing_member, safe_member, unpack_verified
from segio_archive.mail_import import build_local_catalog
from segio_archive.web import CollectionError, Config

FILENAMES=('PRIVATE_'+'x'*71+' '+'report.pdf','y'*79+'.'+'report.pdf',
           'CON.'+'z'*75+' '+'report.pdf')
PAYLOAD=b'SYNTHETIC_ATTACHMENT_NOT_MALWARE'


class LegacyAttachmentPathTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DateIncrementalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def message(self):
        message=EmailMessage()
        message['Subject']='Legacy long attachment names'
        message.set_content('Normal synthetic body')
        for name in FILENAMES:
            message.add_attachment(PAYLOAD,maintype='application',subtype='octet-stream',filename=name)
        (self.fixture.mail/'legacy.eml').write_bytes(message.as_bytes())

    def legacy_backup(self, blocked=False):
        self.message()
        old=self.fixture.export(2)
        from segio_archive import file_ops
        replace=file_ops.replace_file
        def block(source,target,*args):
            name=str(target).replace('\\','/')
            if blocked and '/attachments/1-' in name and name.endswith(' '):
                error=PermissionError(errno.EACCES,'PRIVATE_SECURITY_MESSAGE')
                error.winerror=225
                raise error
            return replace(source,target,*args)
        with patch.object(portable_mail,'safe_name',side_effect=lambda value,limit=80:legacy_mail_attachment_name(value)),patch.object(file_ops,'replace_file',side_effect=block):
            build_local_catalog(old,'employee')
        self.assertTrue(verify_archive(old)['ok'])
        baseline=DateBaseline(old,'employee')
        self.fixture.old,self.fixture.base=old,baseline
        self.fixture.old_inodes={entry[2][:2] for entry in fixtures.worker.inventory(str(self.fixture.mail))}
        paths=[name for name in baseline.manifest['files'] if legacy_attachment_alias(name) is not None]
        self.assertEqual(len(paths),2 if blocked else 3)
        return old,baseline,paths

    def test_new_truncated_names_are_windows_safe_in_full_mail_rendering(self):
        for name in FILENAMES+('CON'+' '*77+'rest.pdf','.'*100,'x'*78+'. '+'rest.pdf'):
            with self.subTest(name=name):
                result=safe_name(name,80)
                safe_member('mail/offline/mail-000000000/attachments/1-'+result)
                self.assertFalse(result.endswith((' ','.')))
        self.message()
        target=self.fixture.export(2)
        build_local_catalog(target,'employee')
        for name in json.loads((target/'manifest.json').read_bytes())['files']:
            safe_member(name)
        self.assertTrue(verify_archive(target)['ok'])

    def test_completed_legacy_paths_increment_in_place_without_changing_old_files(self):
        old,baseline,paths=self.legacy_backup()
        before=self.fixture.snapshot()
        for name in paths:
            with self.assertRaises(CollectionError):safe_member(name)
            self.assertEqual(existing_member(name).as_posix(),name)
        (self.fixture.mail/'new.eml').write_bytes(b'Subject: Next incremental arrival\n\nnew')
        target=self.fixture.export(3,baseline)
        self.fixture.prepare(target,baseline)
        self.assertEqual(before,self.fixture.snapshot())
        self.assertEqual(search(target,kind='mail')['total'],5)
        for name in paths:
            self.assertEqual((target/name).read_bytes(),PAYLOAD)
        self.assertEqual(baseline.commit(target,lambda:None,lambda _:None),old)
        self.assertTrue(verify_archive(old)['ok'])
        self.assertIsInstance(DateBaseline(old,'employee'),DateBaseline)

    def test_old_quarantine_and_missing_attachment_names_do_not_get_new_payload_copies(self):
        old,baseline,paths=self.legacy_backup(blocked=True)
        (old/paths[0]).unlink()
        blocked={name for path in old.glob('mail/offline/*/attachment-status.json')
                 for name in json.loads(path.read_bytes())['skipped']}
        protected={paths[0]}|blocked
        protected|={legacy_attachment_alias(name) for name in protected}
        before=self.fixture.snapshot()
        target=self.fixture.export(3,baseline)
        write=portable_mail.write_generated
        def no_new_attachments(root,name,*args):
            if name in protected:
                raise AssertionError('Old attachment payload must not be recreated under either name')
            return write(root,name,*args)
        with patch.object(portable_mail,'write_generated',side_effect=no_new_attachments):
            self.fixture.prepare(target,baseline)
        self.assertEqual(before,self.fixture.snapshot())
        manifest=json.loads((target/'manifest.json').read_bytes())
        self.assertEqual(manifest['portable_mail']['unavailable_attachment_count'],2)
        self.assertFalse((target/paths[0]).exists())
        self.assertFalse((target/legacy_attachment_alias(paths[0])).exists())
        statuses=list(target.glob('mail/offline/*/attachment-status.json'))
        skipped={name:reason for path in statuses for name,reason in json.loads(path.read_bytes())['skipped'].items()}
        self.assertEqual(sorted(skipped.values()),['attachment_security','attachment_unavailable'])
        self.assertTrue(verify_archive(target)['ok'])

    def test_legacy_attachment_without_cached_state_is_verified_and_reused(self):
        old,baseline,paths=self.legacy_backup()
        directory=(old/paths[0]).parent.parent
        (directory/'state.json').unlink()
        manifest=json.loads((old/'manifest.json').read_bytes())
        identity=directory.name
        record,source=mail_record(old,identity)
        before={name:digest_file(old/name) for name in paths}
        with source.open('rb') as stream:
            portable_mail.render_record(old,manifest,stream,record,lambda:None)
        for name in paths:
            self.assertEqual(digest_file(old/name),before[name])
            if os.name!='nt':
                self.assertFalse((old/legacy_attachment_alias(name)).exists())

    def test_untrusted_zip_and_unrelated_unsafe_paths_still_fail(self):
        name='mail/offline/mail-000000000/attachments/1-'+'x'*79+' '
        existing_member(name)
        target=self.fixture.root/'untrusted'
        target.mkdir()
        archive=make_archive(self.fixture.root,contents={'index.html':b'index',name:PAYLOAD})
        with self.assertRaises(CollectionError):unpack_verified(archive,target,'employee')
        for invalid in ('mail/raw/'+'x'*79+' ', 'documents/1/'+'x'*79+'.',
                        'mail/offline/mail-000000000/attachments/1-short ',
                        'mail/offline/mail-000000000/attachments/1-'+'x'*78+': ',
                        '../outside','C:stream','file:ads','AUX.txt'):
            with self.subTest(path=invalid),self.assertRaises(CollectionError):
                existing_member(invalid)

    def test_legacy_alias_collision_stops_before_login(self):
        old,baseline,paths=self.legacy_backup()
        path=old/'manifest.json'
        manifest=json.loads(path.read_bytes())
        manifest['files'][legacy_attachment_alias(paths[0])]=manifest['files'][paths[0]]
        path.write_text(json.dumps(manifest))
        before=self.fixture.snapshot()
        with self.assertRaises(CollectionError) as caught:DateBaseline(old,'employee')
        self.assertEqual(caught.exception.path_diagnostic,{'reason':'alias_collision','file_kind':'mail_attachment'})
        self.assertEqual(before,self.fixture.snapshot())

    def test_other_invalid_paths_fail_preflight_and_record_only_the_kind_and_reason(self):
        old=self.fixture.old
        path=old/'manifest.json'
        manifest=json.loads(path.read_bytes())
        manifest['files']['documents/1/PRIVATE_FILENAME.']={'bytes':0,'sha256':'a'*64}
        path.write_text(json.dumps(manifest))
        before=self.fixture.snapshot()
        output=self.fixture.root/'output'
        client=DBCollector(Config('https://example.invalid/','employee',output,incremental_base=old))
        with patch('segio_archive.db_client.launch_browser') as browser:
            with self.assertRaises(CollectionError):client.run('PRIVATE_PASSWORD')
        browser.assert_not_called()
        record=json.loads((output/'last-client-failure.json').read_bytes())
        self.assertEqual(record['error'],'INVALID_BACKUP_PATH')
        self.assertEqual(record['path'],{'reason':'trailing_character','file_kind':'approval_document'})
        self.assertNotIn('PRIVATE',json.dumps(record))
        self.assertEqual(before,self.fixture.snapshot())

    def test_local_pending_wrapper_preserves_safe_path_diagnostics(self):
        client,opener,record,payload=downloads.WindowsFileRecoveryTests().download(self.fixture.root)
        def fail(*args):
            client.phase='기존 백업 반영 준비'
            safe_member('documents/1/PRIVATE_FILENAME.')
        with patch('segio_archive.db_client.download_opener',return_value=opener),patch.object(client,'finalize_download',side_effect=fail):
            with self.assertRaisesRegex(CollectionError,'LOCAL_INDEX_PENDING') as caught:
                client.receive(record)
        path=write_failure(self.fixture.root/'diagnostics',caught.exception,'기존 백업 반영 준비')
        value=json.loads(path.read_bytes())
        self.assertEqual(value['path'],{'reason':'trailing_character','file_kind':'approval_document'})
        self.assertNotIn('PRIVATE',path.read_text())
