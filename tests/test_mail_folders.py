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
from segio_archive.compact_mail import digest_file,search
from segio_archive.mail_folders import MailFolders,classify
from segio_archive.portable_mail import prepare_archive
from segio_archive.search_page import install_search_page
from segio_archive.viewer import Viewer
import test_portable_mail as portable_fixture
import test_search_page as search_fixture

BROWSER=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')


class MailFolderTests(unittest.TestCase):
    def fixture(self,root):
        manifest=search_fixture.SearchPageTests().fixture(root)
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute("UPDATE items SET folders='inbox.mbox' WHERE kind='mail' AND folders='받은 편지함'")
            db.execute("UPDATE items SET folders='sentbox' WHERE folders='보낸 편지함'")
            db.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)',[
                ('mail-000001102','mail','계약 문서','거래처/계약.mbox','업무 내용','mail/mail-000001102'),
                ('mail-000001103','mail','연구 문서','연구 & 개발.mbox','업무 내용','mail/mail-000001103')])
            db.commit()
        sources={'sources':[{'name':name,'format':'mbox','bytes':0} for name in
                 ('inbox.mbox','sentbox','거래처/계약.mbox','연구 & 개발.mbox','비어있는함.mbox')]}
        (root/'mail/sources.json').write_text(json.dumps(sources,ensure_ascii=False),encoding='utf-8')
        manifest['counts']['mail']=1104
        for name in ('catalog.sqlite3','mail/sources.json'):
            manifest['files'][name]=digest_file(root/name)
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        return manifest

    def test_source_folder_classification_preserves_names_and_hierarchy(self):
        for name in ('inbox','INBOX.mbox','받은 편지함'):
            self.assertEqual(classify(name)['id'],'base:inbox')
        for name in ('sentbox','Sentbox.mbox','보낸편지함'):
            self.assertEqual(classify(name)['id'],'base:sentbox')
        self.assertEqual(classify('거래처/계약.mbox'),
                         {'id':'custom:거래처/계약','label':'거래처 / 계약','category':'custom'})
        self.assertEqual(classify('보관함/계약/0001.eml','rfc822')['label'],'보관함 / 계약')
        self.assertEqual(classify('.Sent/cur/0001','rfc822')['id'],'base:sentbox')
        self.assertEqual(classify('message.eml','rfc822')['id'],'other:unknown')
        self.assertEqual(classify('sentbox/고객.mbox')['category'],'custom')
        self.assertEqual(classify('Sent.mbox','mbox')['category'],'custom')
        self.assertEqual(classify('sent box.mbox','mbox')['category'],'custom')
        self.assertEqual(classify('보낸편지함.mbox','mbox')['category'],'custom')

    def test_counts_empty_mailboxes_and_search_use_original_folder_not_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            original=self.fixture(root)
            install_search_page(root)
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                boxes={box['id']:box for box in MailFolders(root).boxes(db)}
            self.assertEqual(boxes['base:inbox']['count'],1101)
            self.assertEqual(boxes['base:sentbox']['count'],1)
            self.assertEqual(boxes['custom:거래처/계약']['count'],1)
            self.assertEqual(boxes['custom:비어있는함']['count'],0)
            self.assertEqual(sum(box['count'] for box in boxes.values()),1104)
            sent=search(root,kind='mail',mailbox='base:sentbox')
            self.assertEqual(sent['total'],1)
            self.assertEqual(sent['items'][0]['folder'],'보낸편지함')
            self.assertEqual(sent['items'][0]['source_folder'],'sentbox')
            self.assertEqual(search(root,'받은편지함','mail')['total'],1101)
            self.assertEqual(search(root,'계약','mail',mailbox='custom:거래처/계약')['total'],1)
            self.assertEqual(search(root,kind='mail',mailbox="' OR 1=1 --")['total'],0)
            self.assertEqual(search(root,kind='approval',mailbox='base:sentbox')['total'],1)
            self.assertEqual(digest_file(root/'catalog.sqlite3'),original['files']['catalog.sqlite3'])
            self.assertTrue(verify_archive(root)['ok'])

    def test_existing_portable_backup_upgrade_never_reads_mbox_or_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            portable_fixture.PortableMailTests().fixture(root)
            prepare_archive(root)
            manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            manifest['browser_index']='offline-search/2'
            (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            previous={name:info for name,info in manifest['files'].items() if not name.startswith('search/') and name!='index.html'}
            original_open=Path.open
            def guarded(path,*args,**kwargs):
                if path.suffix=='.mbox' or 'attachments' in path.parts:
                    self.fail('Mailbox classification must not read raw MBOX or attachments')
                return original_open(path,*args,**kwargs)
            with patch.object(Path,'open',guarded):
                prepare_archive(root)
            updated=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(updated['browser_index'],'offline-search/8')
            for name,info in previous.items():
                self.assertEqual(digest_file(root/name),info)
            self.assertTrue(verify_archive(root)['ok'])

    @unittest.skipUnless(BROWSER,'Real Chromium required')
    def test_mailbox_filters_counts_search_and_reload_work_in_file_and_http_browser(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            install_search_page(root)
            viewer=Viewer(root)
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        for url in (viewer.url,(root/'index.html').as_uri()):
                            with self.subTest(url=url):
                                page=browser.new_page()
                                search_fixture.navigate(self,page,url)
                                expect(page.locator('#mail-folders')).to_be_hidden()
                                page.locator('#tab-mail').click()
                                expect(page.locator('#mail-folders')).to_be_visible()
                                expect(page.locator('article')).to_have_count(100)
                                page.get_by_role('button',name='보낸편지함 (1건)',exact=True).click()
                                expect(page.locator('article')).to_have_count(1)
                                expect(page.locator('.mailbox-name')).to_have_text('보낸편지함')
                                page.get_by_role('button',name='거래처 / 계약 (1건)',exact=True).click()
                                expect(page.locator('article')).to_have_count(1)
                                expect(page.locator('article h2')).to_have_text('계약 문서')
                                page.locator('#q').fill('없는 내용')
                                page.locator('#search-button').click()
                                expect(page.locator('#empty')).to_have_text('검색 결과가 없습니다.')
                                page.locator('#q').fill('업무 내용')
                                page.locator('#q').press('Enter')
                                expect(page.locator('article')).to_have_count(1)
                                page.locator('#q').fill('')
                                page.locator('#search-button').click()
                                page.get_by_role('button',name='연구 & 개발 (1건)',exact=True).click()
                                expect(page.locator('article')).to_have_count(1)
                                expect(page.locator('article h2')).to_have_text('연구 문서')
                                page.reload()
                                expect(page.locator('article')).to_have_count(1)
                                expect(page.locator('article h2')).to_have_text('연구 문서')
                                expect(page.get_by_role('button',name='연구 & 개발 (1건)',exact=True)).to_have_attribute('aria-pressed','true')
                                page.get_by_role('button',name='비어있는함 (0건)',exact=True).click()
                                expect(page.locator('article')).to_have_count(0)
                                page.get_by_role('button',name='받은편지함 (1,101건)',exact=True).click()
                                expect(page.locator('article')).to_have_count(100)
                                page.locator('#next').click()
                                expect(page.locator('#page')).to_have_text('2 / 12')
                                page.locator('#tab-approval').click()
                                expect(page.locator('#mail-folders')).to_be_hidden()
                                expect(page.locator('article')).to_have_count(1)
                                page.close()
                    finally:browser.close()
            finally:viewer.close()
