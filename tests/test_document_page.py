import base64
from contextlib import closing
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from urllib.parse import quote
from urllib.request import urlopen

from bs4 import BeautifulSoup

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file
from segio_archive.document_page import signature_image, upgrade_documents
from segio_archive.search_page import install_search_page
from segio_archive.viewer import Viewer

BROWSER = os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')


class DocumentPageTests(unittest.TestCase):
    def fixture(self, root):
        jpeg = (Path(__file__).parent/'fixtures/signature.jpg').read_bytes()
        encoded = base64.b64encode(jpeg).decode()
        signatures = [quote(encoded, safe=''),
                      html.escape('<img src="data:image/jpeg;base64,'+encoded+'">'),
                      json.dumps(encoded.replace('/', '\\/')),
                      base64.b64encode(b'unsupported image bytes'*500).decode()]
        raw = {'source':'active','document':{'wdate':'1700000000'},'related':{'approval_line':[
            {'no':str(index+1),'order_no':str(index),'sign':value} for index,value in enumerate(signatures)]}}
        directory = root/'documents/1'
        directory.mkdir(parents=True)
        (root/'index.html').write_text('Old index')
        (directory/'database.json').write_text(json.dumps(raw))
        (directory/'document.html').write_text('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'"></head><body>'
            '<p class="navigation"><a href="../../index.html">보관함 목록</a> · <a href="database.json">원본 DB 기록</a></p>'
            '<table><tr><th>순서</th><th>서명</th></tr>'+''.join(
            '<tr><td>'+str(index)+'</td><td>'+html.escape(value)+'</td></tr>' for index,value in enumerate(signatures))+
            '</table><a href="attachments/sample.txt">첨부 보기</a></body></html>')
        (directory/'attachments').mkdir()
        (directory/'attachments/sample.txt').write_text('SYNTHETIC_ATTACHMENT_CONTENT')
        (directory/'attachments/unsafe.html').write_text('<script>alert(1)</script>')
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('CREATE TABLE items (id TEXT PRIMARY KEY, kind TEXT,title TEXT,folders TEXT,body TEXT,html TEXT)')
            db.execute('CREATE TABLE document_records (id TEXT PRIMARY KEY,payload TEXT)')
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',('1','approval','결재 예시','결재','내용','documents/1/document.html'))
            db.execute('INSERT INTO document_records VALUES (?,?)',('1',json.dumps(raw)))
            db.commit()
        files={path.relative_to(root).as_posix():digest_file(path) for path in root.rglob('*') if path.is_file()}
        manifest={'viewer':'local-db/1','user':'employee','files':files}
        (root/'manifest.json').write_text(json.dumps(manifest))
        return manifest

    def test_encoded_raster_signatures_recover_without_executing_markup(self):
        jpeg=(Path(__file__).parent/'fixtures/signature.jpg').read_bytes()
        encoded=base64.b64encode(jpeg).decode()
        expected='data:image/jpeg;base64,'+encoded
        for value in (encoded,quote(encoded,safe=''),quote(quote(encoded,safe=''),safe=''),
                      'data:image/jpeg;base64,'+encoded,
                      '<p>'+encoded+'</p>',json.dumps(encoded).replace('/','\\/')):
            with self.subTest(encoding=value[:20]):
                self.assertEqual(signature_image(value),expected)
        for value in ('<svg onload="evil()"/>','data:image/svg+xml;base64,'+encoded,
                      base64.b64encode(b'<script>evil()</script>').decode(), 'https://external.invalid/image'):
            self.assertIsNone(signature_image(value))

    def test_existing_document_update_preserves_originals_and_manifest_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            original=self.fixture(root)
            install_search_page(root)
            page=BeautifulSoup((root/'documents/1/document.html').read_text(),'html.parser')
            self.assertEqual(len(page.select('td img')),3)
            self.assertEqual(page.select('tr')[-1].find_all('td')[-1].get_text(),'[서명 이미지 미확보]')
            for label in ('보관함 목록','원본 DB 기록'):
                link=page.find('a',attrs={'aria-label':label})
                self.assertEqual(link.get_text(),'')
                self.assertIsNotNone(link.svg)
            self.assertEqual(page.find('a',string='첨부 보기')['target'],'_blank')
            self.assertIn('noopener',page.find('a',string='첨부 보기')['rel'])
            for name in ('catalog.sqlite3','documents/1/database.json','documents/1/attachments/sample.txt'):
                self.assertEqual(digest_file(root/name),original['files'][name])
            manifest=json.loads((root/'manifest.json').read_text())
            self.assertEqual(manifest['document_presentation']['unresolved_signatures'],1)
            self.assertTrue(verify_archive(root)['ok'])
            first=(root/'documents/1/document.html').read_bytes()
            upgrade_documents(root,manifest)
            self.assertEqual(first,(root/'documents/1/document.html').read_bytes())

    def test_modified_document_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            manifest=self.fixture(root)
            (root/'documents/1/document.html').write_text('modified')
            with self.assertRaisesRegex(ValueError,'무결성'):
                upgrade_documents(root,manifest)
            self.assertEqual((root/'documents/1/document.html').read_text(),'modified')

    def test_damaged_signature_never_expands_to_thousands_of_visible_characters(self):
        from segio_archive.document_page import render_document
        damaged='/9j/INVALID!'+('corrupted?'*1000)
        source='<table><tr><th>서명</th></tr><tr><td>'+damaged+'</td></tr></table>'
        updated,unresolved=render_document(source,{'related':{}})
        text=BeautifulSoup(updated,'html.parser').get_text()
        self.assertNotIn(damaged,text)
        self.assertIn('[서명 이미지 미확보]',text)
        self.assertEqual(unresolved,1)

    @unittest.skipUnless(BROWSER,'Chromium required')
    def test_recovered_signatures_decode_and_attachment_opens_in_new_tab(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        page=browser.new_page()
                        page.goto(viewer.url.replace('index.html','documents/1/document.html'))
                        expect(page.get_by_role('link',name='보관함 목록')).to_have_count(1)
                        page.wait_for_function('Array.from(document.querySelectorAll("td img")).every(img=>img.complete&&img.naturalWidth>0)')
                        self.assertEqual(page.locator('td img').count(),3)
                        with page.expect_popup() as opened:
                            page.get_by_role('link',name='첨부 보기').click()
                        popup=opened.value
                        expect(popup.locator('body')).to_contain_text('SYNTHETIC_ATTACHMENT_CONTENT')
                        self.assertTrue(page.url.endswith('document.html'))
                        self.assertTrue(popup.evaluate('window.opener===null'))
                    finally:
                        browser.close()
                for name,disposition in (('sample.txt','inline;'),('unsafe.html','attachment;')):
                    with urlopen(viewer.url.replace('index.html','documents/1/attachments/'+name)) as response:
                        self.assertTrue(response.headers['Content-Disposition'].startswith(disposition))
            finally:
                viewer.close()


if __name__=='__main__':
    unittest.main()
