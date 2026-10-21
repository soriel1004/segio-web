from contextlib import closing
from email.message import EmailMessage
import hashlib
import http.client
import json
import os
import shutil
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import build_compact_catalog,mbox_spans,search,mail_record
from segio_archive.ssh_client import SSHConfig,SSHCollector
from segio_archive.viewer import Viewer
from segio_archive.web import Cancelled,CollectionError
from live_fixtures import synthetic_export
from test_live_archive import create_fixture
import remote_ssh_export as helper
from ssh_fixture import SSHServer


class SSHArchiveTests(unittest.TestCase):
    def fixture(self,root):
        settings,original=create_fixture(root)
        server=SSHServer(root)
        self.addCleanup(server.close)
        config=SSHConfig('127.0.0.1','root','employee',root/'windows',port=server.port,
            db_config='/files/database.json',cell='/files',
            mail_template='/files/home/{initial}/{user}/mbox',known_hosts=root/'known_hosts')
        return server,config,original

    def test_ssh_db_and_whole_mbox_export_is_searchable_without_web_or_mail_copy_on_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,original=self.fixture(root)
            fingerprints=[]
            with patch.object(helper,'run_export',side_effect=synthetic_export), \
                 patch('segio_archive.browser.launch_browser',side_effect=AssertionError('SSH must not launch a browser')):
                result=SSHCollector(config,confirm_host=lambda *args:fingerprints.append(args) or True).run('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(verify_archive(result)['ok'])
            self.assertEqual((result/'mail/raw/000000.mbox').read_bytes(),original)
            self.assertEqual(search(result,'회의','mail')['total'],1)
            self.assertEqual(search(result,'현재 DB','approval')['total'],1)
            self.assertFalse((config.output/'segio-employee.zip').exists())
            self.assertTrue(config.known_hosts.is_file())
            self.assertEqual(len(fingerprints),1)
            self.assertTrue(fingerprints[0][2].startswith('SHA256:'))
            self.assertEqual(server.actions,['prepare','verify','cleanup'])
            self.assertEqual(list((root/'jobs').glob('.segio-ssh-*')),[])
            self.assertFalse((result/'mail/messages').exists())
            for path in result.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'SYNTHETIC_SSH_PASSWORD',path.read_bytes())
            viewer=Viewer(result)
            self.addCleanup(viewer.close)
            connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port)
            def get(path):
                connection.request('GET','/'+viewer.token+'/'+path)
                response=connection.getresponse()
                return response.status,response.getheader('Content-Disposition'),response.read()
            try:
                status,_,payload=get('api/search?q='+quote('첨부 확인')+'&kind=mail')
                self.assertEqual(status,200)
                item=json.loads(payload)['items'][0]
                status,_,page=get(item['html'])
                self.assertEqual(status,200)
                self.assertIn('원본 첨부 보존 시험',page.decode())
                self.assertTrue(item['html'].endswith('/index.html'))
                from bs4 import BeautifulSoup
                from urllib.parse import urljoin
                attachment_url=BeautifulSoup(page,'html.parser').select_one('a.attachment')['href']
                status,disposition,attachment=get(urljoin(item['html'],attachment_url))
                self.assertEqual(status,200)
                self.assertEqual(attachment,b'SYNTHETIC_ATTACHMENT')
                self.assertTrue(disposition.startswith('inline;'))
                status,_,eml=get('mail/'+item['id']+'/original.eml')
                self.assertEqual(status,200)
                self.assertIn(b'Content-Type: multipart/mixed',eml)
            finally:
                connection.close()
            executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')
            if executable:
                from playwright.sync_api import sync_playwright,expect
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=executable)
                    try:
                        page=browser.new_page()
                        requests=[]
                        page.on('request',lambda request:requests.append(request.url))
                        page.goto(viewer.url)
                        expect(page.locator('#tab-approval')).to_have_attribute('aria-selected','true')
                        expect(page.locator('article')).to_have_count(1)
                        self.assertIn('현재 DB',page.locator('article').inner_text())
                        self.assertEqual(page.locator('body').evaluate('element=>getComputedStyle(element).fontSize'),'12px')
                        page.locator('#tab-mail').click()
                        expect(page.locator('article')).to_have_count(3)
                        page.locator('#q').fill('회의')
                        page.locator('#search-button').click()
                        expect(page.locator('article')).to_have_count(1)
                        self.assertIn('회의',page.locator('article').inner_text())
                        self.assertTrue(all(url.startswith(viewer.url.rsplit('/',1)[0]) for url in requests))
                        page.locator('#q').fill('없는 검색어')
                        page.locator('#q').press('Enter')
                        expect(page.locator('article')).to_have_count(0)
                        expect(page.locator('#empty')).to_have_text('검색 결과가 없습니다.')
                        page.locator('#q').fill('')
                        page.locator('#search-button').click()
                        expect(page.locator('article')).to_have_count(3)
                    finally:
                        browser.close()

    def test_cancelled_sftp_transfer_resumes_same_snapshot_at_nonzero_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,original=self.fixture(root)
            source=root/'home/e/employee/mbox/large.mbox'
            payload=b'From test@example.test Tue Oct 20 10:00:00 2026\nSubject: Large fixture\n\n'+b'x'*(3*1024*1024)
            source.write_bytes(payload)
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            checkpoint=collector.checkpoint
            def stop_when_partial():
                if config.output.exists():
                    for partial in config.output.glob('.segio-ssh-cache/*/contents/mail/raw/*'):
                        if 1024*1024<=partial.stat().st_size<len(payload):
                            config.cancel.set()
                checkpoint()
            collector.checkpoint=stop_when_partial
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                with self.assertRaises(Cancelled):
                    collector.run('SYNTHETIC_SSH_PASSWORD')
                self.assertEqual(server.actions.count('prepare'),1)
                self.assertTrue(list(config.output.glob('.segio-ssh-cache/*/session.json')))
                config.cancel=threading.Event()
                reads=len(server.reads)
                result=SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(verify_archive(result)['ok'])
            self.assertEqual(server.actions.count('prepare'),1)
            resumed_offsets=[offset for path,offset,_ in server.reads[reads:] if path.endswith('large.mbox')]
            self.assertEqual(min(resumed_offsets),1024*1024)
            self.assertIn(payload,[p.read_bytes() for p in (result/'mail/raw').iterdir()])

    def test_unknown_host_and_wrong_password_do_not_start_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            with self.assertRaisesRegex(CollectionError,'SSH_HOST_KEY_UNCONFIRMED'):
                SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            with self.assertRaisesRegex(CollectionError,'SSH_AUTH_FAILED') as caught:
                SSHCollector(config,confirm_host=lambda *_:True).run('PRIVATE_WRONG_PASSWORD')
            self.assertNotIn('PRIVATE_WRONG_PASSWORD',str(caught.exception))
            self.assertEqual(server.actions,[])

    def test_changed_host_key_is_rejected_before_credentials_or_new_job(self):
        import paramiko
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
            original=list(server.actions)
            server.key=paramiko.RSAKey.generate(2048)
            with self.assertRaisesRegex(CollectionError,'SSH_HOST_KEY_CHANGED'):
                SSHCollector(config,confirm_host=lambda *_:self.fail('Changed host must not be auto-trusted')).run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(server.actions,original)

    def test_mail_only_empty_files_and_optional_zip(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            (root/'home/e/employee/mbox/empty.mbox').touch()
            config.approvals=False
            config.make_zip=True
            with patch.object(helper,'run_export') as database:
                result=SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
                database.assert_not_called()
            self.assertTrue(verify_archive(result)['ok'])
            with zipfile.ZipFile(result.with_suffix('.zip')) as zipped:
                self.assertIn('catalog.sqlite3',zipped.namelist())
            self.assertTrue(any(p.stat().st_size==0 for p in (result/'mail/raw').iterdir()))

    def test_changed_mail_is_not_published_as_completed_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            execute=collector.execute
            def mutate(action,detach=False):
                if action=='verify':
                    with (root/'home/e/employee/mbox/inbox.mbox').open('ab') as stream:
                        stream.write(b'new incoming mail')
                return execute(action,detach)
            collector.execute=mutate
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                with self.assertRaisesRegex(CollectionError,'메일이 변경'):
                    collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertFalse((config.output/'segio-employee').exists())
            self.assertTrue(list(config.output.glob('.segio-ssh-cache/*/session.json')))


class CompactMailTests(unittest.TestCase):
    def test_mbox_delimiter_split_between_chunks_is_indexed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'large.mbox'
            first=b'From first@example.test\nSubject: First\n\n'
            filler=b'x'*(1024*1024-3-len(first))
            second=b'\nFrom second@example.test\nSubject: Second\n\nbody\n'
            path.write_bytes(first+filler+second)
            spans=list(mbox_spans(path))
            self.assertEqual(len(spans),2)
            with path.open('rb') as stream:
                stream.seek(spans[1][0])
                self.assertTrue(stream.read(30).startswith(b'Subject: Second'))

    def test_sqlite_mail_offsets_above_four_gib_are_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            (root/'mail/raw').mkdir(parents=True)
            source=root/'mail/raw/000000.mbox'
            offset=2**32+4096
            payload=b'Subject: Sparse offset\n\nbody\n'
            with source.open('wb') as stream:
                if os.name=='nt':
                    import ctypes,msvcrt
                    from ctypes import wintypes
                    control=ctypes.windll.kernel32.DeviceIoControl
                    control.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,
                                      wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),wintypes.LPVOID]
                    returned=wintypes.DWORD()
                    if not control(msvcrt.get_osfhandle(stream.fileno()),0x900c4,None,0,None,0,ctypes.byref(returned),None):
                        self.skipTest('Sparse file support required for offset-only test')
                stream.seek(offset)
                stream.write(payload)
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                db.execute('CREATE TABLE items (id TEXT,title TEXT)')
                db.execute('CREATE TABLE mail_records (id TEXT,source TEXT,begin INTEGER,end INTEGER,sender TEXT,receiver TEXT,date TEXT)')
                db.execute('INSERT INTO items VALUES (?,?)',('mail-000000000','Sparse offset'))
                db.execute('INSERT INTO mail_records VALUES (?,?,?,?,?,?,?)',('mail-000000000','mail/raw/000000.mbox',offset,offset+len(payload),'','',''))
                db.commit()
            record,path=mail_record(root,'mail-000000000')
            self.assertEqual(record['begin'],offset)
            with path.open('rb') as stream:
                stream.seek(record['begin'])
                self.assertEqual(stream.read(record['end']-record['begin']),payload)
            server=SSHServer(root)
            self.addCleanup(server.close)
            config=SSHConfig('127.0.0.1','root','employee',root/'unused',port=server.port,known_hosts=root/'known_hosts')
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            try:
                collector.connect('SYNTHETIC_SSH_PASSWORD')
                with collector.sftp.open('/files/mail/raw/000000.mbox','rb') as stream:
                    stream.seek(offset)
                    self.assertEqual(stream.read(len(payload)),payload)
            finally:
                if collector.sftp:
                    collector.sftp.close()
                if collector.client:
                    collector.client.close()


if __name__=='__main__':
    unittest.main()
