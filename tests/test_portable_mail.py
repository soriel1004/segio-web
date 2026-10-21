from contextlib import closing
from email.message import EmailMessage
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file
from segio_archive.portable_mail import prepare_archive,page_path,BLOCK
from segio_archive.viewer import Viewer
from segio_archive.web import Cancelled
from test_search_page import navigate

BROWSER=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aVv0AAAAASUVORK5CYII=')


class PortableMailTests(unittest.TestCase):
    def fixture(self,root,large=False):
        raw=root/'mail/raw/000000.mbox'
        raw.parent.mkdir(parents=True)
        first=EmailMessage()
        first['Subject']='HTML 회의 안내'
        first['From']='sender@example.test'
        first['To']='receiver@example.test'
        first.set_content('HTML이 없는 경우의 본문')
        first.add_alternative('<h2>회의 본문</h2><table><tr><td>한글 표 내용</td></tr></table><img src="cid:fixture"><img src="https://external.invalid/pixel"><script>window.UNSAFE=1</script><a href="javascript:alert(1)" onclick="evil()">주소</a>',subtype='html')
        first.get_payload()[1].add_related(PNG,maintype='image',subtype='png',cid='<fixture>',filename='image.png')
        first.add_attachment(b'SYNTHETIC_ATTACHMENT',maintype='application',subtype='octet-stream',filename='../../CON.txt')
        second=(b'Subject: =?euc-kr?b?vcPH6A==?=\r\nContent-Type: text/plain; charset=cp949\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n'+
                __import__('quopri').encodestring('두 번째 본문\n'.encode('cp949')))
        spans=[]
        with raw.open('wb') as stream:
            for payload in (first.as_bytes(),second):
                stream.write(b'From synthetic@example.test Tue Oct 20 10:00:00 2026\n')
                start=stream.tell()
                stream.write(payload)
                spans.append((start,stream.tell()))
                stream.write(b'\n')
            if large:
                stream.write(b'From synthetic@example.test Tue Oct 20 10:00:00 2026\n')
                start=stream.tell()
                stream.write(b'Subject: Large mail\nContent-Type: multipart/mixed; boundary=large\n\n--large\nContent-Type: text/plain; charset=utf-8\n\n'+ '대용량 첨부가 있는 본문\n'.encode()+b'--large\nContent-Type: application/octet-stream\nContent-Disposition: attachment; filename=large.bin\nContent-Transfer-Encoding: base64\n\n')
                line=base64.b64encode(b'x'*57)+b'\r\n'
                for _ in range(450):
                    stream.write(line*1000)
                stream.write(b'\r\n--large--\n')
                spans.append((start,stream.tell()))
        (root/'index.html').write_text('OLD INDEX',encoding='utf-8')
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('CREATE TABLE items (id TEXT PRIMARY KEY,kind TEXT,title TEXT,folders TEXT,body TEXT,html TEXT)')
            db.execute('CREATE TABLE mail_records (id TEXT PRIMARY KEY,source TEXT,begin INTEGER,end INTEGER,sender TEXT,receiver TEXT,date TEXT)')
            for number,(start,end) in enumerate(spans):
                identity=f'mail-{number:09d}'
                title=('HTML 회의 안내','실행','Large mail')[number]
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',(identity,'mail',title,'받은 편지함','sender@example.test','mail/'+identity))
                db.execute('INSERT INTO mail_records VALUES (?,?,?,?,?,?,?)',(identity,'mail/raw/000000.mbox',start,end,'sender@example.test','receiver@example.test','2026-10-21'))
            db.commit()
        manifest={'viewer':'sqlite-mail/1','browser_index':'offline-search/1','counts':{'mail':len(spans)},'files':{}}
        for name in ('index.html','catalog.sqlite3','mail/raw/000000.mbox'):
            manifest['files'][name]=digest_file(root/name)
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        return manifest

    def test_existing_mbox_converts_to_static_html_attachments_and_inline_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            old=self.fixture(root)
            prepare_archive(root)
            self.assertTrue(verify_archive(root)['ok'])
            for name in ('catalog.sqlite3','mail/raw/000000.mbox'):
                self.assertEqual(digest_file(root/name),old['files'][name])
            page=root/page_path('mail-000000000')
            body=page.read_text(encoding='utf-8')
            soup=BeautifulSoup(body,'html.parser')
            self.assertIn('한글 표 내용',soup.get_text())
            self.assertNotIn('HTML이 없는 경우의 본문',soup.select_one('main').get_text())
            self.assertIsNone(soup.select_one('script'))
            self.assertNotIn('external.invalid',body)
            self.assertNotIn('javascript:',body)
            from urllib.parse import unquote
            image=page.parent/unquote(soup.select_one('main img')['src'])
            self.assertEqual(image.read_bytes(),PNG)
            payloads=[(page.parent/unquote(a['href'])).read_bytes() for a in soup.select('a.attachment')]
            self.assertIn(b'SYNTHETIC_ATTACHMENT',payloads)
            self.assertIn('두 번째 본문',(root/page_path('mail-000000001')).read_text(encoding='utf-8'))
            previous=page.stat().st_mtime_ns
            prepare_archive(root)
            self.assertEqual(page.stat().st_mtime_ns,previous)

    def test_message_over_32mib_keeps_body_and_streams_attachment_with_bounded_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root,large=True)
            original_open=Path.open
            class Bounded:
                def __init__(self,stream):self.stream=stream
                def read(self,size=-1):
                    self_test.assertTrue(0<=size<=BLOCK)
                    return self.stream.read(size)
                def readline(self,size=-1):
                    self_test.assertTrue(0<=size<=BLOCK)
                    return self.stream.readline(size)
                def __getattr__(self,key):return getattr(self.stream,key)
            self_test=self
            def guarded(path,*args,**kwargs):
                stream=original_open(path,*args,**kwargs)
                return Bounded(stream) if path==root/'mail/raw/000000.mbox' else stream
            with patch.object(Path,'open',guarded):
                prepare_archive(root)
            page=root/page_path('mail-000000002')
            self.assertIn('대용량 첨부가 있는 본문',page.read_text(encoding='utf-8'))
            attachment=next((page.parent/'attachments').glob('*-large.bin'))
            expected=hashlib.sha256()
            for _ in range(450):expected.update(b'x'*57000)
            self.assertEqual(digest_file(attachment),{'bytes':25650000,'sha256':expected.hexdigest()})
            self.assertTrue(verify_archive(root)['ok'])

    def test_cancelled_conversion_reuses_completed_mail_without_server_or_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            old=self.fixture(root)
            state=root/'mail/offline/mail-000000000/state.json'
            def checkpoint():
                if state.exists():raise Cancelled('synthetic interruption')
            with self.assertRaises(Cancelled):prepare_archive(root,checkpoint)
            page=state.parent/'index.html'
            os.utime(page,ns=(1000000000,1000000000))
            prepare_archive(root)
            self.assertEqual(page.stat().st_mtime_ns,1000000000)
            self.assertTrue(verify_archive(root)['ok'])
            self.assertEqual(digest_file(root/'mail/raw/000000.mbox'),old['files']['mail/raw/000000.mbox'])

    def test_full_disk_does_not_replace_index_or_mark_partial_conversion_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            old=self.fixture(root)
            original_open=Path.open
            def full_disk(path,*args,**kwargs):
                if path.suffix=='.partial':raise OSError(28,'No space left on device')
                return original_open(path,*args,**kwargs)
            with patch.object(Path,'open',full_disk):
                with self.assertRaises(OSError):prepare_archive(root)
            self.assertEqual(digest_file(root/'index.html'),old['files']['index.html'])
            self.assertNotIn('portable_mail',json.loads((root/'manifest.json').read_text(encoding='utf-8')))
            self.assertTrue(verify_archive(root)['ok'])

    @unittest.skipUnless(BROWSER,'Real Chromium required')
    def test_index_click_opens_mail_and_attachment_in_separate_tab(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            prepare_archive(root)
            # HTTP also exercises rendering on cloud machines blocking file URLs.
            viewer=Viewer(root)
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        for url in (viewer.url,(root/'index.html').as_uri()):
                            with self.subTest(url=url):
                                if url.startswith('file:'):viewer.close()
                                page=browser.new_page()
                                requests=[]
                                page.on('request',lambda request:requests.append(request.url))
                                navigate(self,page,url)
                                expect(page.locator('article')).to_have_count(2)
                                page.locator('article a').first.click()
                                expect(page.locator('main')).to_contain_text('한글 표 내용')
                                self.assertTrue(page.url.endswith('/index.html'))
                                self.assertEqual(page.locator('main img').evaluate('image=>image.complete && image.naturalWidth'),1)
                                self.assertEqual(page.locator('body').evaluate('node=>getComputedStyle(node).fontSize'),'12px')
                                link=page.get_by_role('link',name='../../CON.txt')
                                original_url=page.url
                                with page.expect_popup() as opened:
                                    link.click()
                                popup=opened.value
                                expect(popup.locator('body')).to_contain_text('SYNTHETIC_ATTACHMENT')
                                self.assertEqual(page.url,original_url)
                                popup.close()
                                page.get_by_role('link',name='메일 목록',exact=True).click()
                                expect(page.locator('#tab-mail')).to_have_attribute('aria-selected','true')
                                expect(page.locator('article')).to_have_count(2)
                                self.assertTrue(all(request.startswith(('file:',viewer.url.rsplit('/',1)[0])) for request in requests))
                                page.close()
                    finally:browser.close()
            finally:
                # close() is safe only once; file test deliberately stopped it.
                if viewer.thread.is_alive():viewer.close()
