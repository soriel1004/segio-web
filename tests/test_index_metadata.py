from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file, search
from segio_archive.index_metadata import approval_categories, approval_date, header_date
from segio_archive.search_page import install_search_page
from segio_archive.viewer import Viewer

BROWSER = os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')


class IndexMetadataTests(unittest.TestCase):
    def fixture(self, root):
        (root/'index.html').write_text('Old index')
        (root/'mail').mkdir()
        (root/'mail/raw.mbox').write_bytes(b'ORIGINAL_MAIL_MUST_NOT_CHANGE')
        (root/'mail/sources.json').write_text(json.dumps({'sources':[
            {'name':name,'format':'mbox'} for name in ('inbox.mbox','sentbox','고객/계약.mbox')]}))
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('CREATE TABLE items (id TEXT PRIMARY KEY,kind TEXT,title TEXT,folders TEXT,body TEXT,html TEXT)')
            db.execute('CREATE TABLE document_records (id TEXT PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE mail_dates (id TEXT PRIMARY KEY,header_date TEXT)')
            for identity, title, progress, extra in (
                    ('1','완료 계약','완료',{'reference':'employee@@이름','wdate':'1000000000'}),
                    ('2','반려 계약','rejected',{'wdate':'1700000000'}),
                    ('3','수신 계약','999',{'doc_recv_uid':'employee','cdate':'1900000000'}),
                    ('4','진행 계약','pending',{'wdate':'1600000000'})):
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                           (identity,'approval',title,'결재 문서','내용','documents/'+identity+'/document.html'))
                db.execute('INSERT INTO document_records VALUES (?,?)',
                           (identity,json.dumps({'document':dict(extra,progress=progress),'related':{}})))
            for identity,title,folder,date in (
                    ('mail-1','오래된 받은메일','inbox.mbox','Tue, 01 Jan 2002 10:30:00 +0900'),
                    ('mail-2','새로운 보낸메일','sentbox','Wed, 01 Jan 2020 22:00:00 +0900'),
                    ('mail-3','분류된 받은메일','고객/계약.mbox','bad date')):
                # Backup/move date must never be used as the message Date.
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                           (identity,'mail',title,json.dumps([folder]),'sender\nreceiver\n2030-01-01 backup',identity+'.html'))
                db.execute('INSERT INTO mail_dates VALUES (?,?)',(identity,date))
            db.commit()
        document_files=[]
        for identity in ('1','2','3','4'):
            name='documents/'+identity+'/document.html'
            path=root/name
            path.parent.mkdir(parents=True)
            path.write_text('<!doctype html><meta charset="utf-8"><p>결재 문서</p>')
            document_files.append(name)
        manifest={'format':'segio-db-html/1','user':'employee','finished_at':'2030-01-01T00:00:00Z',
                  'files':{name:digest_file(root/name) for name in
                           ['index.html','catalog.sqlite3','mail/raw.mbox','mail/sources.json']+document_files}}
        (root/'manifest.json').write_text(json.dumps(manifest))
        return manifest

    def test_approval_roles_overlap_and_unknown_numeric_codes_are_not_guessed(self):
        value={'document':{'progress':'완료','reference':'employee2@@다른 사람,employee@@이름',
                           'doc_recv_uid':'other'},
               'related':{'approval_send_box':[{'recv_uid':'employee'}]}}
        self.assertEqual(approval_categories(value,'employee'),['completed','reference','received'])
        self.assertEqual(approval_categories(value,'employ'),['completed'])
        self.assertEqual(approval_categories({'document':{'progress':999,'cdate':1700000000}},'employee'),['unknown'])
        self.assertEqual(approval_categories({'document':{'progress':'999'}},'employee',{'999':'rejected'}),['rejected'])
        self.assertEqual(approval_categories({'document':{'progress':0}},'employee',{'0':'completed'}),['completed'])

    def test_header_date_uses_header_timezone_and_does_not_invent_missing_dates(self):
        self.assertEqual(header_date('Tue, 01 Jan 2002 10:30:00 +0900')[0],'2002-01-01 10:30 +0900')
        self.assertEqual(header_date('Tue, 01 Jan 2002 01:30:00 +0000')[1],
                         header_date('Tue, 01 Jan 2002 10:30:00 +0900')[1])
        self.assertEqual(header_date('bad date'),('날짜 미상',None))
        self.assertEqual(header_date(''),('날짜 미상',None))

    def test_draft_date_uses_wdate_not_completion_or_backup_time(self):
        self.assertEqual(approval_date({'document':{'wdate':'1700000000'}}),
                         ('2023-11-15 07:13:20 KST',1700000000))
        self.assertEqual(approval_date({'document':{'wdate':'0','cdate':'1900000000'}}),
                         ('기안일자 미상',None))
        self.assertEqual(approval_date({'document':{'wdate':'2023-11-15 07:13:20'}})[1],1700000000)

    def test_queries_and_offline_chunks_preserve_sources_and_exclude_capture_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            original=self.fixture(root)
            install_search_page(root)
            self.assertEqual(search(root,kind='approval',category='completed')['total'],1)
            self.assertEqual(search(root,kind='approval',category='reference')['items'][0]['title'],'완료 계약')
            self.assertEqual(search(root,kind='approval',category='received')['items'][0]['categories'],['unknown','received'])
            self.assertEqual(search(root,kind='approval',category="' OR 1=1 --")['total'],0)
            rows=search(root,kind='mail')['items']
            self.assertEqual([row['id'] for row in rows],['mail-2','mail-1','mail-3'])
            self.assertEqual(rows[1]['date'],'2002-01-01 10:30 +0900')
            self.assertEqual(rows[2]['date'],'날짜 미상')
            self.assertEqual(search(root,kind='mail',mailbox='custom:고객/계약')['total'],1)
            chunks=[]
            for path in sorted((root/'search').glob('part-*.js')):
                chunks.extend(json.loads(path.read_text()[len('window.archiveChunk='):-1]))
            self.assertEqual([row[0] for row in chunks if row[1]=='mail'],['mail-2','mail-1','mail-3'])
            self.assertEqual(next(row for row in chunks if row[0]=='mail-1')[8],rows[1]['date'])
            self.assertNotIn('2030',rows[1]['date'])
            self.assertEqual(digest_file(root/'catalog.sqlite3'),original['files']['catalog.sqlite3'])
            self.assertEqual(digest_file(root/'mail/raw.mbox'),original['files']['mail/raw.mbox'])
            self.assertTrue(verify_archive(root)['ok'])

    def test_latest_drafts_appear_on_first_page_in_api_and_static_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            manifest=self.fixture(root)
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                for index in range(105):
                    identity='extra-'+str(index)
                    db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                               (identity,'approval',identity,'결재','내용','documents/1/document.html'))
                    db.execute('INSERT INTO document_records VALUES (?,?)',
                               (identity,json.dumps({'document':{'wdate':1800000000+index}})))
                db.commit()
            manifest['files']['catalog.sqlite3']=digest_file(root/'catalog.sqlite3')
            (root/'manifest.json').write_text(json.dumps(manifest))
            install_search_page(root)
            first=search(root,kind='approval')['items']
            self.assertEqual(len(first),100)
            self.assertEqual(first[0]['id'],'extra-104')
            self.assertEqual(first[-1]['id'],'extra-5')
            last=search(root,kind='approval',page=1)['items']
            self.assertEqual(last[-1]['id'],'3')
            self.assertEqual(last[-1]['date'],'기안일자 미상')
            chunks=[]
            for path in sorted((root/'search').glob('part-*.js')):
                chunks.extend(json.loads(path.read_text()[len('window.archiveChunk='):-1]))
            self.assertEqual([row[0] for row in chunks[:100]],[item['id'] for item in first])

    @unittest.skipUnless(BROWSER,'Chromium required')
    def test_browser_top_menus_header_dates_and_offline_search_match_http(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                html=(root/'index.html').read_text()
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        for offline in (False,True):
                            with self.subTest(offline_chunks=offline):
                                page=browser.new_page()
                                requests=[]
                                page.on('request',lambda request:requests.append(request.url))
                                if offline:
                                    # Managed Chromium blocks file:// itself. Exercise its
                                    # exact chunk-script search branch over the local server.
                                    page.route('**/index.html',lambda route:route.fulfill(
                                        content_type='text/html',body=html.replace("offline=location.protocol==='file:'",'offline=true')))
                                page.goto(viewer.url)
                                expect(page.locator('article h2').first).to_have_text('반려 계약')
                                expect(page.locator('#date-label')).to_have_text('기안일자')
                                expect(page.locator('.message-date').first).to_have_text('2023-11-15 07:13:20 KST')
                                expect(page.locator('#reset')).to_have_count(0)
                                page.get_by_role('button',name='반려문서 (1건)',exact=True).click()
                                expect(page.locator('article')).to_have_count(1)
                                expect(page.locator('article h2')).to_have_text('반려 계약')
                                page.get_by_role('button',name='참조문서 (1건)',exact=True).click()
                                expect(page.locator('article h2')).to_have_text('완료 계약')
                                page.get_by_role('button',name='수신문서 (1건)',exact=True).click()
                                expect(page.locator('article h2')).to_have_text('수신 계약')
                                page.reload()
                                expect(page.get_by_role('button',name='수신문서 (1건)',exact=True)).to_have_attribute('aria-pressed','true')
                                page.locator('#tab-mail').click()
                                page.get_by_role('button',name='받은편지함 (1건)',exact=True).click()
                                expect(page.locator('.message-date')).to_have_text('2002-01-01 10:30 +0900')
                                page.get_by_role('button',name='고객 / 계약 (1건)',exact=True).click()
                                expect(page.locator('article h2')).to_have_text('분류된 받은메일')
                                expect(page.locator('.message-date')).to_have_text('날짜 미상')
                                page.locator('#q').fill('없는 제목')
                                page.locator('#search-button').click()
                                expect(page.locator('article')).to_have_count(0)
                                page.locator('#q').fill('')
                                page.get_by_role('button',name='전체 메일 (3건)',exact=True).click()
                                page.locator('#search-button').click()
                                expect(page.locator('article')).to_have_count(3)
                                page.locator('#q').fill('계약')
                                for scope,total in (('',5),('approval',4),('mail',1)):
                                    page.locator('#scope').select_option(scope)
                                    page.locator('#search-button').click()
                                    expect(page.locator('article')).to_have_count(total)
                                    expect(page.locator('#summary')).to_contain_text(str(total)+'건')
                                if offline:
                                    self.assertFalse(any('/api/search?' in url for url in requests))
                                page.close()
                    finally:
                        browser.close()
            finally:
                viewer.close()


if __name__=='__main__':
    unittest.main()
