from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file, search
from segio_archive.mail_import import build_local_catalog, header_text
from segio_archive.portable_mail import page_path, prepare_archive, ISSUE_REPORT
from segio_archive.web import CollectionError

NORMAL=b'Subject: Normal mail\nFrom: sender@example.test\nContent-Type: text/plain\n\nNormal body\n'
BAD_NAME=(b'Subject: Encoded filename\nContent-Type: application/octet-stream\n'
          b'Content-Disposition: attachment; filename="=?utf-8?b?A?="\nContent-Transfer-Encoding: base64\n\nQUJD\n')
BAD_BODY=(b'Subject: Broken attachment <script>PRIVATE</script>\nContent-Type: multipart/mixed; boundary=parts\n\n'
          b'--parts\nContent-Type: application/octet-stream\nContent-Disposition: attachment; filename=good.bin\n'
          b'Content-Transfer-Encoding: base64\n\nQUJD\n'
          b'--parts\nContent-Type: application/octet-stream\nContent-Disposition: attachment; filename=bad.bin\n'
          b'Content-Transfer-Encoding: base64\n\nA\n--parts--\n')


class MalformedMailTests(unittest.TestCase):
    def test_invalid_base64_is_not_silently_published_as_empty_or_truncated_attachment(self):
        import io
        from segio_archive.portable_mail import decoded,MailFormatError
        for data in (b'A',b'!!!!',b'QQ==QUJD',b'QQ==A',b'Q?Q=='):
            with self.subTest(data=data):
                with self.assertRaises(MailFormatError):
                    b''.join(decoded(io.BytesIO(data),0,len(data),'base64',lambda:None))
        for data,expected in ((b'Q Q\r\n==',b'A'),(b'QUJDZA',b'ABCd'),(b'QUJDZA=',b'ABCd')):
            self.assertEqual(b''.join(decoded(io.BytesIO(data),0,len(data),'base64',lambda:None)),expected)
    def fixture(self,root,payloads):
        raw=root/'mail/raw/000000.bin'
        raw.parent.mkdir(parents=True)
        data=b''.join(b'From fixture@example.test Thu Oct 22 10:00:00 2026\n'+value+b'\n' for value in payloads)
        raw.write_bytes(data)
        (root/'index.html').write_text('Import pending')
        (root/'catalog.json').write_text('{"items":[]}')
        info=digest_file(raw)
        (root/'mail/sources.json').write_text(json.dumps({'user':'employee','sources':[
            dict(info,name='inbox.mbox',path='mail/raw/000000.bin')]}))
        manifest={'format':'segio-db-html/1','user':'employee','account_deletion_verified':False,
                  'state':'collected_with_warnings','counts':{'approval':0},'warnings':[],'coverage':[],
                  'live_export':{'job':'a'*32},'local_processing':{'state':'pending'},'files':{}}
        for name in ('index.html','catalog.json','mail/sources.json','mail/raw/000000.bin'):
            manifest['files'][name]=digest_file(root/name)
        (root/'manifest.json').write_text(json.dumps(manifest))
        return data

    def test_invalid_encoded_header_and_surrogate_have_safe_visible_fallbacks(self):
        issues=set()
        self.assertEqual(header_text('=?utf-8?b?A?=',issues),'=?utf-8?b?A?=')
        self.assertIn('header_encoding',issues)
        self.assertEqual(header_text('bad\udcff',issues),'bad\\udcff')
        self.assertIn('header_unicode',issues)

    def test_bad_headers_filename_base64_and_mime_do_not_block_following_mail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            raw=self.fixture(root,[NORMAL,b'Subject: =?utf-8?b?A?=\n\nHeader fallback\n',BAD_NAME,BAD_BODY,
                b'Subject: Missing boundary\nContent-Type: multipart/mixed; boundary=missing\n\nNo delimiter\n',
                NORMAL.replace(b'Normal mail',b'Last normal mail')])
            result=build_local_catalog(root,'employee')
            self.assertEqual(result['counts']['mail'],6)
            self.assertEqual(result['portable_mail']['warning_count'],4)
            self.assertEqual(result['portable_mail']['raw_only_count'],2)
            self.assertEqual((root/'mail/raw/000000.mbox').read_bytes(),raw)
            self.assertTrue(verify_archive(root)['ok'])
            self.assertEqual(search(root,'Last normal mail',kind='mail')['total'],1)
            self.assertEqual(search(root,'=?utf-8?b?A?=',kind='mail')['total'],1)
            attachment=next((root/page_path('mail-000000002')).parent.glob('attachments/*'))
            self.assertEqual(attachment.read_bytes(),b'ABC')
            for number in (3,4):
                identity=f'mail-{number:09d}'
                folder=(root/page_path(identity)).parent
                self.assertFalse((folder/'attachments').exists())
                self.assertIn('완료하지 못했습니다',(folder/'index.html').read_text())
                with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                    begin,end=db.execute('SELECT begin,end FROM mail_records WHERE id=?',(identity,)).fetchone()
                self.assertEqual((folder/'original.eml').read_bytes(),raw[begin:end])
            report=(root/ISSUE_REPORT).read_text()
            self.assertIn('Broken attachment &lt;script&gt;PRIVATE&lt;/script&gt;',report)
            self.assertNotIn('<script>',report)
            self.assertEqual(report.count('<a href="offline/'),4)
            self.assertFalse(list(root.rglob('*.partial')))

    def test_io_failure_is_not_downgraded_to_mail_warning_and_local_recovery_reuses_valid_pages(self):
        import segio_archive.file_ops as ops
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            raw=self.fixture(root,[NORMAL,BAD_BODY,NORMAL])
            replace=ops.replace_file
            def denied(source,target,*args):
                if target.name=='original.eml':raise PermissionError('PRIVATE_ACCESS_DENIED')
                return replace(source,target,*args)
            with patch.object(ops,'replace_file',side_effect=denied):
                with self.assertRaises(PermissionError):build_local_catalog(root,'employee')
            self.assertEqual((root/'mail/raw/000000.mbox').read_bytes(),raw)
            self.assertEqual(json.loads((root/'manifest.json').read_bytes())['local_processing']['state'],'pending')
            first=root/page_path('mail-000000000')
            before=first.stat().st_mtime_ns
            # A 0.5.9 state has the same source/files fingerprint, no new fields.
            state_path=first.parent/'state.json'
            state=json.loads(state_path.read_bytes())
            state.pop('issues',None);state.pop('raw_only',None)
            state_path.write_text(json.dumps(state))
            result=prepare_archive(root)
            self.assertEqual(first.stat().st_mtime_ns,before)
            self.assertNotIn('local_processing',result)
            self.assertEqual(result['portable_mail']['raw_only_count'],1)
            self.assertTrue(verify_archive(root)['ok'])
            self.assertEqual((root/'mail/raw/000000.mbox').read_bytes(),raw)

    def test_unsupported_encoding_and_deep_mime_are_explicit_raw_only_records(self):
        deep=b'Content-Type: text/plain\n\nbody\n'
        for number in range(23):
            boundary=('b%d'%number).encode()
            deep=b'Content-Type: multipart/mixed; boundary='+boundary+b'\n\n--'+boundary+b'\n'+deep+b'\n--'+boundary+b'--\n'
        cases=[b'Subject: Unsupported\nContent-Transfer-Encoding: unsupported\n\nbody\n',b'Subject: Deep\n'+deep]
        for payload in cases:
            with self.subTest(case=payload[:30]),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve()
                self.fixture(root,[payload,NORMAL])
                result=build_local_catalog(root,'employee')
                self.assertEqual(result['portable_mail']['raw_only_count'],1)
                self.assertEqual(result['counts']['mail'],2)
                self.assertTrue(verify_archive(root)['ok'])

    def test_unexpected_processing_failure_keeps_original_and_saves_only_safe_origin(self):
        from test_windows_file_recovery import WindowsFileRecoveryTests
        from segio_archive.client_diagnostics import write_failure
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            client,opener,record,payload=WindowsFileRecoveryTests().download(root)
            with patch('segio_archive.db_client.download_opener',return_value=opener),patch(
                    'segio_archive.portable_mail.render_record',side_effect=RuntimeError('PRIVATE_MAIL_PASSWORD_PATH')):
                with self.assertRaisesRegex(CollectionError,'LOCAL_INDEX_PENDING') as caught:client.receive(record)
            path=write_failure(client.config.output,caught.exception,client.phase)
            text=path.read_text()
            value=json.loads(text)
            self.assertEqual(value['processing']['exception_type'],'RuntimeError')
            self.assertEqual(value['processing']['site']['module'],'portable_mail')
            self.assertEqual(value['processing']['site']['function'],'build_portable_mail')
            self.assertNotIn('PRIVATE',text)
            self.assertNotIn('employee',text)
            self.assertNotIn(str(root),text)
            backup=next(client.config.output.glob('segio-employee*'))
            self.assertEqual((backup/'mail/raw/000000.mbox').read_bytes(),payload)
            prepare_archive(backup)
            self.assertTrue(verify_archive(backup)['ok'])
            opener.open.assert_called_once()

    @unittest.skipUnless(os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'),'Chromium required')
    def test_offline_list_links_to_safe_clickable_warning_report(self):
        from playwright.sync_api import sync_playwright,expect
        from segio_archive.viewer import Viewer
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root,[BAD_BODY,NORMAL])
            build_local_catalog(root,'employee')
            viewer=Viewer(root)
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
                    try:
                        page=browser.new_page()
                        page.goto(viewer.url)
                        expect(page.locator('#notice')).to_contain_text('1건은 형식 확인')
                        page.get_by_role('link',name='확인 필요 메일 목록').click()
                        expect(page.locator('h1')).to_have_text('메일 변환 확인 사항')
                        self.assertEqual(page.locator('script').count(),0)
                        page.locator('a[href^="offline/"]').click()
                        expect(page.locator('.note')).to_contain_text('본문·첨부 자동 변환은 완료하지 못했습니다')
                    finally:browser.close()
            finally:viewer.close()
