from contextlib import closing
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

import test_date_incremental as fixtures
from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file,search
from segio_archive.search_page import install_search_page
from segio_archive.viewer import Viewer

BROWSER=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')


@unittest.skipUnless(BROWSER,'Chromium required')
class DeletePageTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DateIncrementalTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.old

    def test_select_cancel_confirm_and_search_right_edge_in_live_browser(self):
        from playwright.sync_api import sync_playwright,expect
        viewer=Viewer(self.root)
        self.addCleanup(viewer.close)
        original=self.fixture.snapshot()
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=BROWSER)
            try:
                page=browser.new_page();page.goto(viewer.url)
                expect(page.locator('#delete-button')).to_be_disabled()
                page.locator('#tab-all').click()
                expect(page.locator('article')).to_have_count(4)
                delete_box=page.locator('#delete-button').bounding_box()
                search_box=page.locator('#search-button').bounding_box()
                self.assertGreater(delete_box['x'],search_box['x'])
                page.locator('article[data-identity="approval-1"] input').check()
                page.locator('article[data-identity="mail-000000000"] input').check()
                page.locator('#delete-button').click()
                expect(page.locator('#delete-description')).to_contain_text('2건')
                page.locator('#delete-cancel').click()
                self.assertEqual(original,self.fixture.snapshot())
                page.locator('#delete-button').click()
                expect(page.locator('#delete-confirm')).to_be_enabled()
                page.locator('#delete-confirm').click()
                expect(page.locator('#delete-description')).to_contain_text('삭제 완료',timeout=30000)
                page.locator('#delete-cancel').click()
                expect(page.locator('article')).to_have_count(2)
                expect(page.locator('#tab-all')).to_contain_text('2건')
                self.assertTrue(verify_archive(self.root)['ok'])
            finally:browser.close()

    def test_more_than_30000_matching_rows_are_selected_without_rendering_all_rows(self):
        from playwright.sync_api import sync_playwright,expect
        count=31009
        manifest=json.loads((self.root/'manifest.json').read_bytes())
        catalog=json.loads((self.root/'catalog.json').read_bytes())
        original=catalog['items'][0]
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as db:
            for number in range(count):
                identity='bulk-'+str(number)
                item=dict(original,id=identity,title='Bulk '+str(number),search='Bulk')
                catalog['items'].append(item)
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',('approval-'+identity,'approval',item['title'],'결재 문서','Bulk',item['path']))
                db.execute('INSERT INTO document_records VALUES (?,?)',('approval-'+identity,'{}'))
            db.commit()
        (self.root/'catalog.json').write_text(json.dumps(catalog))
        for name in ('catalog.json','catalog.sqlite3'):manifest['files'][name]=digest_file(self.root/name)
        manifest.pop('browser_index',None)
        (self.root/'manifest.json').write_text(json.dumps(manifest))
        install_search_page(self.root,manifest)
        viewer=Viewer(self.root);self.addCleanup(viewer.close)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=BROWSER)
            try:
                page=browser.new_page();page.goto(viewer.url)
                page.locator('#q').fill('Bulk');page.locator('#search-button').click()
                expect(page.locator('#summary')).to_contain_text('31,009건')
                expect(page.locator('article')).to_have_count(100)
                page.locator('#select-matches').click()
                expect(page.locator('#selection-count')).to_contain_text('31,009건')
                page.locator('.item-select').first.uncheck()
                expect(page.locator('#selection-count')).to_contain_text('31,008건')
                page.locator('#delete-button').click()
                expect(page.locator('#delete-description')).to_contain_text('31,008건',timeout=30000)
                page.locator('#delete-confirm').click()
                expect(page.locator('#delete-description')).to_contain_text('삭제 완료',timeout=90000)
                page.locator('#delete-cancel').click()
                expect(page.locator('article')).to_have_count(1)
                expect(page.locator('#q')).to_have_value('Bulk')
                self.assertEqual(search(self.root,kind='approval')['total'],2)
                self.assertTrue((self.root/original['path']).is_file())
                self.assertTrue(verify_archive(self.root)['ok'])
            finally:browser.close()

    def test_file_origin_bridge_transfers_selection_but_needs_a_separate_local_confirmation(self):
        from playwright.sync_api import sync_playwright,expect
        from segio_archive.delete_page import SCRIPT
        from segio_archive.native_archive import parse_url
        source=Viewer(self.root)
        self.addCleanup(source.close)
        launched=[]
        def launch(uri):
            folder,token,port=parse_url(uri)
            viewer=Viewer(folder,bridge_token=token,port=port,bridge=True)
            launched.append(viewer)
        html=(self.root/'index.html').read_text().replace("offline=location.protocol==='file:'",'offline=true')
        html=html.replace("script-src 'self' file:","script-src 'self' file: http://127.0.0.1:*")
        script=SCRIPT.replace("location.href=uri;","window.testLaunchURI(uri);")
        script=script.replace("archive:location.href.split('#')[0]",'archive:'+json.dumps((self.root/'index.html').as_uri()))
        script=script.replace("if(location.protocol==='file:')",'if(true)').replace("if(location.protocol!=='file:')",'if(false)')
        original=self.fixture.snapshot()
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=BROWSER)
            try:
                page=browser.new_page()
                events=[]
                page.on('console',lambda message:events.append(re.sub(r'http://127\.0\.0\.1:[^ ]+', 'LOOPBACK', message.text)[:220]) if message.type=='error' else None)
                page.on('request',lambda request:events.append('REQUEST '+request.method+' port='+str(urlsplit(request.url).port)+' origin='+str(request.headers.get('origin'))) if '/bridge/' in request.url else None)
                page.on('response',lambda response:events.append('RESPONSE '+str(response.status)+' port='+str(urlsplit(response.url).port)) if '/bridge/' in response.url else None)
                page.expose_function('testLaunchURI',launch)
                page.route('**/index.html',lambda route:route.fulfill(content_type='text/html',body=html,
                    headers={'Content-Security-Policy':'sandbox allow-scripts allow-modals allow-popups allow-popups-to-escape-sandbox'}))
                page.route('**/search/delete.js',lambda route:route.fulfill(content_type='text/javascript',body=script))
                page.route('**/search/part-*.js',lambda route:route.fulfill(content_type='text/javascript',body=(self.root/'search'/urlsplit(route.request.url).path.rsplit('/',1)[1]).read_text()))
                page.goto(source.url)
                expect(page.locator('article')).to_have_count(1)
                page.locator('.item-select').check()
                with page.expect_popup() as created:
                    page.locator('#delete-button').click()
                manager=created.value
                expect(page.locator('#notice')).to_contain_text('PC 삭제 관리 화면',timeout=45000)
                self.assertEqual(len(launched),1)
                expect(manager.locator('#delete-description')).to_contain_text('1건',timeout=30000)
                self.assertEqual(original,self.fixture.snapshot())
                manager.locator('#delete-confirm').click()
                expect(manager.locator('#delete-description')).to_contain_text('삭제 완료',timeout=30000)
                self.assertFalse((self.root/'documents/1').exists())
            finally:
                browser.close()
                for viewer in launched:viewer.close()
