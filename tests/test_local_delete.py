from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode,urlsplit

import test_date_incremental as fixtures
from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file,search
from segio_archive.date_incremental import DateBaseline
from segio_archive.local_delete import DeletePlan,catalog_items,TOMBSTONES
from segio_archive.native_archive import parse_url
from segio_archive.viewer import Viewer


class LocalDeleteTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DateIncrementalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.old

    def plan(self,ids=None,all_matches=False,filters=None):
        plan=DeletePlan(self.root)
        self.addCleanup(plan.close)
        ids=ids or []
        for position in range(0,len(ids),500):plan.add(ids[position:position+500])
        plan.seal(all_matches,filters)
        return plan

    def test_delete_removes_selected_records_and_individual_files_and_keeps_shared_mbox(self):
        raw={item['path']:digest_file(self.root/item['path']) for item in self.fixture.base.sources}
        checkpoint=json.loads((self.root/'manifest.json').read_bytes())['mail_checkpoint']
        plan=self.plan(['approval-1','mail-000000000'])
        self.assertEqual(plan.count,2)
        result=plan.run()
        self.assertEqual(result['deleted'],2)
        self.assertFalse((self.root/'documents/1').exists())
        self.assertFalse((self.root/'mail/offline/mail-000000000').exists())
        self.assertEqual(search(self.root,kind='approval')['total'],0)
        self.assertEqual(search(self.root,kind='mail')['total'],2)
        for name,expected in raw.items():self.assertEqual(digest_file(self.root/name),expected)
        manifest=json.loads((self.root/'manifest.json').read_bytes())
        self.assertEqual(manifest['mail_checkpoint'],checkpoint)
        self.assertTrue(verify_archive(self.root)['ok'])
        self.assertIsInstance(DateBaseline(self.root,'employee'),DateBaseline)

    def test_prepare_failure_and_rename_failure_leave_entire_previous_backup_unchanged(self):
        original=self.fixture.snapshot()
        plan=self.plan(['approval-1'])
        with patch('segio_archive.search_page.install_search_page',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):plan.run()
        self.assertEqual(self.fixture.snapshot(),original)
        plan=self.plan(['approval-1'])
        import os
        replace=os.replace
        def reject(source,target):
            if Path(source).name.startswith('.segio-delete-') and Path(target)==self.root:
                raise PermissionError('synthetic commit failure')
            return replace(source,target)
        with patch('segio_archive.file_ops.os.replace',side_effect=reject):
            with self.assertRaises(PermissionError):plan.run()
        self.assertEqual(self.fixture.snapshot(),original)
        self.assertEqual(list(self.root.parent.glob('.segio-delete-*')),[])

    def test_changed_backup_after_preview_does_not_delete_anything(self):
        plan=self.plan(['approval-1'])
        with (self.root/'manifest.json').open('ab') as stream:stream.write(b' ')
        original=self.fixture.snapshot()
        with self.assertRaisesRegex(Exception,'백업이 변경'):plan.run()
        self.assertEqual(self.fixture.snapshot(),original)

    def test_incremental_export_does_not_restore_deleted_document_or_mail(self):
        self.plan(['approval-1','mail-000000000']).run()
        baseline=DateBaseline(self.root,'employee')
        records=json.loads((self.fixture.root/'database.json').read_bytes())
        records['employee']={'no':'1','title':'Deleted approval fetched again','wdate':baseline.approval_since+1}
        (self.fixture.root/'database.json').write_text(json.dumps(records))
        with (self.fixture.mail/'inbox.mbox').open('ab') as stream:
            stream.write(b'\nFrom new@example.test Tue Oct 20 13:00:00 2026\nSubject: New after local deletion\n\nnew\n')
        target=self.fixture.export(2,baseline,changed=['inbox.mbox'])
        self.fixture.prepare(target,baseline)
        self.assertEqual(search(target,kind='approval')['total'],0)
        self.assertFalse((target/'documents/1').exists())
        self.assertEqual(search(target,kind='mail')['total'],3)
        self.assertEqual(search(target,'New after local deletion',kind='mail')['total'],1)
        self.assertTrue(verify_archive(target)['ok'])

    def test_selection_filter_matches_search_and_all_mode_exclusions(self):
        matches=search(self.root,query='회의',kind='mail')
        self.assertEqual(matches['total'],1)
        plan=self.plan(all_matches=True,filters={'q':'회의','kind':'mail'})
        self.assertEqual(plan.count,1)
        plan.run()
        self.assertEqual(search(self.root,'회의',kind='mail')['total'],0)
        self.assertEqual(search(self.root,kind='mail')['total'],2)
        plan=self.plan(['mail-000000001'],all_matches=True,filters={'kind':'mail'})
        self.assertEqual(plan.count,1)
        plan.run()
        self.assertEqual(search(self.root,kind='mail')['items'][0]['id'],'mail-000000001')

    def test_standalone_eml_is_deleted_and_is_not_restored_by_a_later_delta(self):
        (self.fixture.mail/'single.eml').write_bytes(b'Subject: Standalone unique message\n\nbody')
        root=self.fixture.export(2)
        from segio_archive.mail_import import build_local_catalog
        build_local_catalog(root,'employee')
        self.root=self.fixture.old=root
        self.fixture.base=DateBaseline(root,'employee')
        identity=search(root,'Standalone unique message',kind='mail')['items'][0]['id']
        source=next(item for item in self.fixture.base.sources if item['name']=='single.eml')
        self.plan([identity]).run()
        self.assertFalse((root/source['path']).exists())
        self.assertFalse(any(item['name']=='single.eml' for item in json.loads((root/'mail/sources.json').read_bytes())['sources']))
        baseline=DateBaseline(root,'employee')
        target=self.fixture.export(3,baseline,changed=['single.eml'])
        self.fixture.prepare(target,baseline)
        self.assertEqual(search(target,'Standalone unique message',kind='mail')['total'],0)
        self.assertFalse(any(item['name']=='single.eml' for item in json.loads((target/'mail/sources.json').read_bytes())['sources']))
        self.assertTrue(verify_archive(target)['ok'])
        changed=b'Subject: Standalone unique message\n\nDIFFERENT BODY WITH IDENTICAL HEADERS'
        (self.fixture.mail/'single.eml').write_bytes(changed)
        next_baseline=DateBaseline(target,'employee')
        newer=self.fixture.export(4,next_baseline,changed=['single.eml'])
        self.fixture.prepare(newer,next_baseline)
        self.assertEqual(search(newer,'Standalone unique message',kind='mail')['total'],1)
        entry=next(item for item in json.loads((newer/'mail/sources.json').read_bytes())['sources'] if item['name']=='single.eml')
        self.assertEqual((newer/entry['path']).read_bytes(),changed)
        self.assertTrue(verify_archive(newer)['ok'])

    def test_last_mbox_message_stays_deleted_when_append_changes_its_separator_newline(self):
        self.plan(['mail-000000002']).run()
        baseline=DateBaseline(self.root,'employee')
        with (self.fixture.mail/'inbox.mbox').open('ab') as stream:
            stream.write(b'\nFrom reply@example.test Tue Oct 20 13:00:00 2026\nSubject: Newly appended reply\n\nbody\n')
        target=self.fixture.export(2,baseline,changed=['inbox.mbox'])
        self.fixture.prepare(target,baseline)
        self.assertEqual(search(target,'첨부 확인',kind='mail')['total'],0)
        self.assertEqual(search(target,'Newly appended reply',kind='mail')['total'],1)
        self.assertEqual(search(target,kind='mail')['total'],3)
        self.assertTrue(verify_archive(target)['ok'])

    def test_more_than_30000_documents_delete_physical_files_without_sql_parameter_limit(self):
        count=31007
        manifest=json.loads((self.root/'manifest.json').read_bytes())
        catalog=json.loads((self.root/'catalog.json').read_bytes())
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as db:
            for number in range(2,count+2):
                identity=str(number)
                folder=self.root/'documents'/identity
                folder.mkdir()
                body=b'<p>Bulk document</p>'
                (folder/'document.html').write_bytes(body)
                record=b'{"document":{"progress":"completed"}}'
                (folder/'database.json').write_bytes(record)
                page='documents/'+identity+'/document.html'
                raw='documents/'+identity+'/database.json'
                for name,data in ((page,body),(raw,record)):
                    manifest['files'][name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                catalog['items'].append({'id':identity,'title':'Bulk '+identity,'path':page,'json':raw,'search':'Bulk synthetic','attachments':[]})
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',('approval-'+identity,'approval','Bulk '+identity,'결재 문서','Bulk synthetic',page))
                db.execute('INSERT INTO document_records VALUES (?,?)',('approval-'+identity,record.decode()))
            db.commit()
        (self.root/'catalog.json').write_text(json.dumps(catalog))
        manifest['counts']['approval']=count+1
        for name in ('catalog.json','catalog.sqlite3'):manifest['files'][name]=digest_file(self.root/name)
        (self.root/'manifest.json').write_text(json.dumps(manifest))
        plan=self.plan(ids=['approval-'+str(number) for number in range(2,count+2)],filters={'q':'Bulk','kind':'approval'})
        self.assertEqual(plan.count,count)
        phases=[]
        result=plan.run(lambda stage,done,total:phases.append(stage))
        self.assertEqual(result['deleted'],count)
        self.assertEqual(search(self.root,kind='approval')['total'],1)
        self.assertEqual(len(list((self.root/'documents').iterdir())),1)
        self.assertEqual(search(self.root,kind='mail')['total'],3)
        self.assertIn('검색 목록 갱신',phases)
        self.assertTrue(verify_archive(self.root)['ok'])
        with (self.root/TOMBSTONES).open() as stream:self.assertEqual(sum(1 for line in stream),count)


class DeleteHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DateIncrementalTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.viewer=Viewer(self.fixture.old,bridge=True)
        self.addCleanup(self.viewer.close)

    def request(self,action,fields=None,origin='same',token=True,method='POST'):
        viewer=self.viewer
        host='127.0.0.1:'+str(viewer.server.server_port)
        headers={'Content-Type':'application/json','Origin':'http://'+host if origin=='same' else origin}
        if token:headers['X-Archive-Token']=viewer.mutation_token
        if action.startswith('bridge/'):headers['X-Bridge-Token']=viewer.bridge_token
        connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port,timeout=60)
        token=viewer.bridge_token if action.startswith('bridge/') else viewer.token
        connection.request(method,'/'+token+'/'+action,json.dumps(fields or {}) if method=='POST' else None,headers)
        response=connection.getresponse();data=response.read();status=response.status;result_headers=dict(response.getheaders());connection.close()
        return status,data,result_headers

    def test_post_requires_mutation_token_same_origin_and_correct_host_scope(self):
        for origin,token in (('http://foreign.invalid',True),('null',True),('same',False)):
            status,body,headers=self.request('api/edit/create',{'all':True},origin,token)
            self.assertEqual(status,403)
        self.assertEqual(self.request('bridge/start',{},origin='null')[0],403)
        self.assertTrue(verify_archive(self.fixture.old)['ok'])

    def test_bridge_nonce_does_not_grant_read_access_to_backup_data(self):
        viewer=self.viewer
        self.assertNotEqual(viewer.token,viewer.bridge_token)
        for path in ('index.html','search/part-000000.js','api/edit/info'):
            connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port)
            connection.request('GET','/'+viewer.bridge_token+'/'+path,headers={'Origin':'null'})
            response=connection.getresponse()
            self.assertEqual(response.status,404)
            response.read();connection.close()
        connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port)
        connection.request('GET','/'+viewer.token+'/search/part-000000.js',headers={'Sec-Fetch-Site':'cross-site'})
        response=connection.getresponse();self.assertEqual(response.status,403);response.read();connection.close()

    def test_old_display_revision_cannot_select_newer_backup_contents(self):
        status,body,headers=self.request('api/edit/create',{'all':True,'revision':'old'})
        self.assertIn('error',json.loads(body))
        self.assertTrue(verify_archive(self.fixture.old)['ok'])

    def test_bridge_only_transfers_selection_and_cannot_authorize_deletion(self):
        original=self.fixture.snapshot()
        status,body,headers=self.request('bridge/create',{'all':False,'revision':json.loads((self.fixture.old/'manifest.json').read_bytes())['browser_edit_revision']},origin='same',token=False)
        self.assertEqual(status,200)
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        selection=json.loads(body)['selection']
        self.assertEqual(self.request('bridge/add',{'selection':selection,'ids':['approval-1']},origin='same',token=False)[0],200)
        response=json.loads(self.request('bridge/open',{'selection':selection},origin='same',token=False)[1])
        self.assertTrue(response['url'].startswith(self.viewer.url))
        self.assertEqual(self.fixture.snapshot(),original)
        result=json.loads(self.request('api/edit/preview',{'selection':selection})[1])
        self.assertEqual(result['count'],1)
        self.assertEqual(self.request('api/edit/start',{'selection':selection},origin='null')[0],403)
        self.request('api/edit/start',{'selection':selection})
        for attempt in range(200):
            result=json.loads(self.request('api/edit/status?selection='+selection,method='GET')[1])
            if result['state'] in ('done','failed'):break
            time.sleep(.02)
        self.assertEqual(result['state'],'done',result)
        self.assertFalse((self.fixture.old/'documents/1').exists())

    def test_native_url_rejects_network_paths_extra_parameters_and_wrong_tokens(self):
        fields={'archive':(self.fixture.old/'index.html').as_uri(),'token':'a'*64,'port':'52000'}
        value='segio-backup://open?'+urlencode(fields)
        folder,token,port=parse_url(value)
        self.assertEqual(folder,self.fixture.old)
        self.assertEqual(port,52000)
        for changed in ({'archive':'file://external.invalid/share/index.html'},{'token':'bad'},{'port':'80'},{'extra':'x'}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                parse_url('segio-backup://open?'+urlencode(dict(fields,**changed)))
