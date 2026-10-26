from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file, search
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
    def test_page_sizes_offsets_last_page_and_invalid_api_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                endpoint=viewer.url.rsplit('/',1)[0]+'/api/search?kind=mail&'
                self.assertEqual(len(search(root,kind='mail')['items']),100)
                for size in (1,25,37,50,75,100,1000):
                    with self.subTest(size=size):
                        with urlopen(endpoint+f'page_size={size}&page=1') as response:
                            data=json.load(response)
                        self.assertEqual(data['total'],1102)
                        self.assertEqual(len(data['items']),min(size,1102-size))
                        self.assertEqual(data['items'][0]['id'],f'mail-{size:09d}')
                        last=(1102-1)//size
                        data=search(root,kind='mail',page=last,page_size=size)
                        self.assertEqual(len(data['items']),1102-last*size)
                        self.assertEqual(data['items'][-1]['id'],'mail-000001101')
                for value in ('0','-1','1001','1.5','abc','1 OR 1=1'):
                    with self.subTest(invalid=value):
                        from urllib.parse import quote
                        with self.assertRaises(HTTPError) as caught:
                            urlopen(endpoint+'page_size='+quote(value))
                        self.assertEqual(caught.exception.code,400)
                        caught.exception.close()
            finally:viewer.close()

    @unittest.skipUnless(BROWSER,'Chromium is required')
    def test_page_size_presets_custom_input_search_and_tabs_in_both_search_modes(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            manifest=self.fixture(root)
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                db.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)',
                    [(f'extra-{i:03d}','approval',f'추가 결재 {i:03d}','결재 문서','내용','documents/1/index.html') for i in range(130)])
                db.commit()
            manifest['files']['catalog.sqlite3']=digest_file(root/'catalog.sqlite3')
            (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            viewer=Viewer(root)
            try:
                html=(root/'index.html').read_text(encoding='utf-8')
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        for offline in (False,True):
                            with self.subTest(offline_chunks=offline):
                                page=browser.new_page()
                                requests=[]
                                page.on('request',lambda request:requests.append(request.url))
                                if offline:
                                    page.route('**/index.html',lambda route:route.fulfill(content_type='text/html',
                                        body=html.replace("offline=location.protocol==='file:'",'offline=true')))
                                page.goto(viewer.url)
                                expect(page.locator('article')).to_have_count(100)
                                page.locator('#tab-mail').click()
                                expect(page.locator('#summary')).to_contain_text('1,102건')
                                for size in (25,50,75,100):
                                    page.locator('#page-size').select_option(str(size))
                                    expect(page.locator('article')).to_have_count(size)
                                    expect(page.locator('#page')).to_have_text(f'1 / {math.ceil(1102/size)}')
                                    expect(page.locator('#custom-page-size')).to_be_hidden()
                                    page.locator('#next').click()
                                    expect(page.locator('article h2').first).to_have_text(f'메일 {size:04d}')
                                    expect(page.locator('article > span').first).to_have_text(str(size+1))
                                page.locator('#page-size').select_option('custom')
                                input=page.locator('#page-size-input')
                                for invalid in ('','0','-1','1001','2.5'):
                                    input.fill(invalid)
                                    input.press('Enter')
                                    self.assertFalse(input.evaluate('(e)=>e.checkValidity()'))
                                    expect(page.locator('#page')).to_have_text('2 / 12')
                                    expect(page.locator('article')).to_have_count(100)
                                input.fill('37')
                                input.press('Enter')
                                expect(page.locator('article')).to_have_count(37)
                                expect(page.locator('#page')).to_have_text('1 / 30')
                                page.locator('#next').click()
                                expect(page.locator('article h2').first).to_have_text('메일 0037')
                                page.locator('#q').fill('sender@example.test')
                                page.locator('#q').press('Enter')
                                expect(page.locator('#page')).to_have_text('1 / 30')
                                expect(page.locator('article')).to_have_count(37)
                                page.locator('#q').fill('100%_한글')
                                page.locator('#search-button').click()
                                expect(page.locator('article')).to_have_count(1)
                                expect(page.locator('#page')).to_have_text('1 / 1')
                                expect(page.locator('#next')).to_be_disabled()
                                page.locator('#q').fill('')
                                page.locator('#search-button').click()
                                page.locator('#tab-approval').click()
                                expect(page.locator('#summary')).to_contain_text('131건')
                                expect(page.locator('article')).to_have_count(37)
                                page.locator('#tab-all').click()
                                expect(page.locator('#summary')).to_contain_text('1,233건')
                                expect(page.locator('article')).to_have_count(37)
                                input.fill('1000')
                                page.locator('#page-size-form button').click()
                                expect(page.locator('article')).to_have_count(1000)
                                page.locator('#next').click()
                                expect(page.locator('article')).to_have_count(233)
                                expect(page.locator('article > span').first).to_have_text('1001')
                                expect(page.locator('#next')).to_be_disabled()
                                page.locator('#prev').click()
                                expect(page.locator('article')).to_have_count(1000)
                                input.fill('1')
                                page.locator('#page-size-form button').click()
                                expect(page.locator('article')).to_have_count(1)
                                expect(page.locator('#page')).to_have_text('1 / 1233')
                                page.locator('#page-size').select_option('25')
                                expect(page.locator('article')).to_have_count(25)
                                self.assertEqual(any('/api/search?' in url for url in requests),not offline)
                                page.close()
                    finally:browser.close()
            finally:viewer.close()

    @unittest.skipUnless(BROWSER,'Chromium is required')
    def test_title_links_have_no_underline_and_hover_and_focus_highlight_the_row(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        page=browser.new_page()
                        page.goto(viewer.url)
                        link=page.locator('article h2 a').first
                        expect(link).to_be_visible()
                        self.assertEqual(link.evaluate('(e)=>getComputedStyle(e).textDecorationLine'),'none')
                        link.hover()
                        self.assertEqual(page.locator('article').first.evaluate('(e)=>getComputedStyle(e).backgroundColor'),'rgb(234, 243, 255)')
                        link.focus()
                        page.locator('h1').hover()
                        self.assertEqual(page.locator('article').first.evaluate('(e)=>getComputedStyle(e).backgroundColor'),'rgb(234, 243, 255)')
                    finally:browser.close()
            finally:viewer.close()
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
            self.assertEqual(updated['browser_index'],'offline-search/8')
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
