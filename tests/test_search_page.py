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
from segio_archive.compact_mail import digest_file
from segio_archive.search_page import install_search_page
from segio_archive.viewer import Viewer


BROWSER = os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')


def navigate(test, page, url):
    from playwright.sync_api import Error
    try:
        page.goto(url)
    except Error as exc:
        # The managed cloud Chromium blocks all file URLs. Actual file://
        # tests still run with the unrestricted Playwright browser in CI.
        if url.startswith('file:') and 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc):
            test.skipTest('Managed browser policy blocks file:// URLs')
        raise


class SearchPageTests(unittest.TestCase):
    def fixture(self, root, approvals=True):
        (root/'index.html').write_text('Old search page', encoding='utf-8')
        (root/'mail').mkdir()
        (root/'mail/original.mbox').write_bytes(b'ORIGINAL_MBOX_MUST_NOT_BE_READ_OR_CHANGED')
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('CREATE TABLE items (id TEXT PRIMARY KEY,kind TEXT,title TEXT,folders TEXT,body TEXT,html TEXT)')
            if approvals:
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                           ('approval-1','approval','결재 첫 문서','결재 문서','본문 찾기','documents/1/index.html'))
            db.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)',
                [(f'mail-{i:09d}','mail',f'메일 {i:04d}', '받은 편지함',
                  'sender@example.test\nreceiver@example.test','mail/'+f'mail-{i:09d}') for i in range(1101)])
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                       ('mail-000001101','mail','</script><img src="https://external.invalid/pixel"> 100%_한글',
                        '보낸 편지함','sender@example.test','mail/mail-000001101'))
            db.commit()
        manifest={'viewer':'sqlite-mail/1','counts':{'mail':1102},'files':{}}
        for name in ('index.html','catalog.sqlite3','mail/original.mbox'):
            manifest['files'][name]=digest_file(root/name)
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        return manifest

    def test_existing_backup_is_upgraded_without_reading_mail_or_changing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            old=self.fixture(root)
            original_open=Path.open
            def guarded_open(path,*args,**kwargs):
                if path==root/'mail/original.mbox':
                    self.fail('A display update must not read the original MBOX')
                return original_open(path,*args,**kwargs)
            with patch.object(Path,'open',guarded_open):
                viewer=Viewer(root)
                viewer.close()
            updated=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(updated['browser_index'],'offline-search/5')
            self.assertEqual(updated['files']['mail/original.mbox'],old['files']['mail/original.mbox'])
            self.assertEqual(digest_file(root/'catalog.sqlite3'),old['files']['catalog.sqlite3'])
            self.assertTrue(verify_archive(root)['ok'])
            self.assertEqual(len(list((root/'search').glob('part-*.js'))),3)
            before=(root/'index.html').stat().st_mtime_ns
            install_search_page(root)
            self.assertEqual((root/'index.html').stat().st_mtime_ns,before)

    def test_modified_old_index_is_not_overwritten_or_declared_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            (root/'index.html').write_text('modified',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'무결성'):
                install_search_page(root)
            self.assertFalse(verify_archive(root)['ok'])
            self.assertEqual((root/'index.html').read_text(encoding='utf-8'),'modified')

    @unittest.skipUnless(BROWSER,'Chromium is required for real browser archive search')
    def test_direct_file_and_local_viewer_search_tabs_pagination_and_untrusted_titles(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        for url in ((root/'index.html').as_uri(),viewer.url):
                            with self.subTest(url=url):
                                page=browser.new_page()
                                requests=[]
                                page.on('request',lambda request:requests.append(request.url))
                                navigate(self,page,url)
                                expect(page.locator('article')).to_have_count(1)
                                page.locator('#tab-mail').click()
                                expect(page.locator('article')).to_have_count(100)
                                expect(page.locator('#summary')).to_contain_text('1,102건')
                                expect(page.locator('#page')).to_have_text('1 / 12')
                                page.locator('#next').click()
                                expect(page.locator('#page')).to_have_text('2 / 12')
                                expect(page.locator('article h2').first).to_have_text('메일 0100')
                                page.locator('#q').fill('100%_한글')
                                page.locator('#search-button').click()
                                expect(page.locator('article')).to_have_count(1)
                                expect(page.locator('article')).to_contain_text('</script><img')
                                self.assertEqual(page.locator('article img').count(),0)
                                page.locator('#q').fill('sender@example.test')
                                page.locator('#q').press('Enter')
                                expect(page.locator('#summary')).to_contain_text('1,102건')
                                page.locator('#tab-approval').click()
                                expect(page.locator('#empty')).to_have_text('검색 결과가 없습니다.')
                                page.locator('#q').fill('')
                                page.locator('#search-button').click()
                                expect(page.locator('article')).to_have_count(1)
                                page.locator('#q').fill('본문 찾기')
                                page.locator('#q').press('Enter')
                                expect(page.locator('article')).to_have_count(1)
                                self.assertEqual(page.locator('body').evaluate('element=>getComputedStyle(element).fontSize'),'12px')
                                self.assertTrue(all(request.startswith(('file:',viewer.url.rsplit('/',1)[0])) for request in requests))
                                page.close()
                    finally:
                        browser.close()
            finally:
                viewer.close()

    @unittest.skipUnless(BROWSER,'Chromium is required for real browser archive search')
    def test_mail_only_backup_opens_mail_list_without_clicking_a_tab(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root,approvals=False)
            install_search_page(root)
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                try:
                    page=browser.new_page()
                    navigate(self,page,(root/'index.html').as_uri())
                    expect(page.locator('#tab-mail')).to_have_attribute('aria-selected','true')
                    expect(page.locator('article')).to_have_count(100)
                finally:
                    browser.close()
