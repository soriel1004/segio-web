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

    def file_page(self,browser,launch):
        from segio_archive.delete_page import SCRIPT
        from segio_archive.delete_bridge import START_PAGE
        source=Viewer(self.root);self.addCleanup(source.close)
        html=(self.root/'index.html').read_text().replace("offline=location.protocol==='file:'",'offline=true')
        html=html.replace("script-src 'self' file:","script-src 'self' file: http://127.0.0.1:*")
        script=SCRIPT
        script=script.replace("archive:location.href.split('#')[0]",'archive:'+json.dumps((self.root/'index.html').as_uri()))
        script=script.replace("if(location.protocol==='file:')",'if(true)').replace("if(location.protocol!=='file:')",'if(false)')
        page=browser.new_page()
        page.context.expose_function('testLaunchURI',launch)
        page.context.route('**/search/manage.html',lambda route:route.fulfill(content_type='text/html',body=START_PAGE.replace('location.href=uri;','window.testLaunchURI(uri);')))
        page.route('**/index.html',lambda route:route.fulfill(content_type='text/html',body=html,
            headers={'Content-Security-Policy':'sandbox allow-scripts allow-modals allow-popups allow-popups-to-escape-sandbox'}))
        page.route('**/search/delete.js',lambda route:route.fulfill(content_type='text/javascript',body=script))
        page.route('**/search/part-*.js',lambda route:route.fulfill(content_type='text/javascript',body=(self.root/'search'/urlsplit(route.request.url).path.rsplit('/',1)[1]).read_text()))
        def readiness(route):
            path=self.root/'search'/urlsplit(route.request.url).path.rsplit('/',1)[1]
            route.fulfill(status=200 if path.is_file() else 404,content_type='text/javascript',body=path.read_text() if path.is_file() else '')
        page.route('**/search/manage-*.js?*',readiness)
        page.goto(source.url)
        return page

    def start_native(self,uri):
        from segio_archive.native_archive import parse_url,publish_signal,signal_path
        folder,token,port=parse_url(uri)
        self.assertEqual(port,0)
        viewer=Viewer(folder,bridge_token=token,port=0,bridge=True)
        self.addCleanup(viewer.close)
        publish_signal(signal_path(folder,token),token,'ready',port=viewer.server.server_port)
        return viewer

    def open_selection(self,page):
        from playwright.sync_api import expect
        expect(page.locator('article')).to_have_count(1)
        page.locator('.item-select').check()
        with page.expect_popup() as created:page.locator('#delete-button').click()
        manager=created.value
        manager.locator('#manage-launch').click()
        return manager

    def snapshot(self):
        return {name:data for name,data in self.fixture.snapshot().items() if not re.fullmatch(r'search/manage-[a-f0-9]{64}\.js',name)}

    def test_file_origin_bridge_transfers_selection_but_needs_a_separate_local_confirmation(self):
        from playwright.sync_api import sync_playwright,expect
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=BROWSER)
            try:
                page=self.file_page(browser,self.start_native)
                original=self.snapshot()
                manager=self.open_selection(page)
                expect(page.locator('#notice')).to_contain_text('PC 삭제 관리 화면',timeout=30000)
                expect(manager.locator('#delete-description')).to_contain_text('1건',timeout=30000)
                self.assertEqual(original,self.snapshot())
                manager.locator('#delete-confirm').click()
                expect(manager.locator('#delete-description')).to_contain_text('삭제 완료',timeout=30000)
                self.assertFalse((self.root/'documents/1').exists())
            finally:browser.close()

    def test_delayed_program_permission_waits_without_navigating_guessed_ports(self):
        from playwright.sync_api import sync_playwright,expect
        pending=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=BROWSER)
            try:
                page=self.file_page(browser,lambda uri:pending.append(uri))
                page.clock.install()
                manager=self.open_selection(page)
                expect(manager.locator('h1')).to_contain_text('프로그램을 여는 중')
                page.clock.fast_forward(35000)
                expect(page.locator('#delete-progress')).to_contain_text('기다리는 중')
                self.assertIn('/search/manage.html',manager.url)
                viewer=self.start_native(pending[0])
                navigations=[]
                manager.on('framenavigated',lambda frame:navigations.append(frame.url) if frame==manager.main_frame else None)
                expect(page.locator('#notice')).to_contain_text('PC 삭제 관리 화면',timeout=30000)
                expect(manager.locator('#delete-description')).to_contain_text('1건',timeout=30000)
                ports={urlsplit(url).port for url in navigations if url.startswith('http:')}
                self.assertEqual(ports,{viewer.server.server_port})
                self.assertEqual(len([url for url in navigations if '/bridge/connect' in url]),1)
            finally:browser.close()

    def test_cancelled_program_launch_releases_the_ui_and_a_fresh_attempt_connects(self):
        from playwright.sync_api import sync_playwright,expect
        pending=[]
        def launch(uri):
            pending.append(uri)
            if len(pending)>1:self.start_native(uri)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=BROWSER)
            try:
                page=self.file_page(browser,launch)
                original=self.snapshot()
                manager=self.open_selection(page)
                expect(manager.locator('#manage-cancel')).to_have_text('연결 취소')
                manager.locator('#manage-cancel').click()
                expect(page.locator('#delete-dialog')).not_to_be_visible()
                expect(page.locator('#delete-button')).to_be_enabled()
                self.assertTrue(manager.is_closed())
                self.assertEqual(original,self.snapshot())
                with page.expect_popup() as created:page.locator('#delete-button').click()
                created.value.locator('#manage-launch').click()
                expect(page.locator('#notice')).to_contain_text('PC 삭제 관리 화면',timeout=30000)
                expect(created.value.locator('#delete-description')).to_contain_text('1건',timeout=30000)
                self.assertNotEqual(parse_qs(urlsplit(pending[0]).query)['token'],parse_qs(urlsplit(pending[1]).query)['token'])
            finally:browser.close()

    def test_startup_failure_stops_progress_and_does_not_open_any_loopback_error_page(self):
        from playwright.sync_api import sync_playwright,expect
        from segio_archive.native_archive import parse_url,publish_signal,signal_path
        def launch(uri):
            folder,token,port=parse_url(uri)
            publish_signal(signal_path(folder,token),token,'failed',message='PC 프로그램 준비 오류 · 보관함 열기를 사용하세요.')
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=BROWSER)
            try:
                page=self.file_page(browser,launch)
                manager=self.open_selection(page)
                expect(page.locator('#delete-progress')).to_contain_text('PC 프로그램 준비 오류')
                expect(page.locator('#delete-meter')).not_to_be_visible()
                expect(page.locator('#delete-button')).to_be_enabled()
                expect(manager.locator('h1')).to_contain_text('완료하지 못했습니다')
                self.assertIn('/search/manage.html',manager.url)
                self.assertTrue((self.root/'documents/1').is_dir())
            finally:browser.close()

    def test_missing_program_times_out_with_a_close_button_instead_of_endless_progress(self):
        from playwright.sync_api import sync_playwright,expect
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=BROWSER)
            try:
                page=self.file_page(browser,lambda uri:None)
                page.clock.install()
                manager=self.open_selection(page)
                page.clock.fast_forward(181000)
                expect(page.locator('#delete-progress')).to_contain_text('실행을 확인하지 못했습니다')
                expect(page.locator('#delete-meter')).not_to_be_visible()
                expect(page.locator('#delete-cancel')).to_have_text('닫기')
                self.assertIn('/search/manage.html',manager.url)
                page.locator('#delete-cancel').click()
                expect(page.locator('#delete-dialog')).not_to_be_visible()
                expect(page.locator('#delete-button')).to_be_enabled()
            finally:browser.close()
