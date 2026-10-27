"""Draft-time filtering is exercised against a disposable real PostgreSQL DB."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import export_approval_db as exporter
import live_export_worker as worker

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import search
from segio_archive.date_incremental import DateBaseline
from segio_archive.db_client import unpack_verified
from segio_archive.mail_import import build_local_catalog


class ApprovalDateTests(unittest.TestCase):
    def test_invalid_cutoffs_fail_before_any_database_work(self):
        for raw in ('0','-1','1.5','1 OR 1=1','9'*13,True,{},str(int(time.time())+1000)):
            with self.subTest(raw=raw),patch.object(exporter,'query_database') as query:
                with self.assertRaises(exporter.SafeError) as error:
                    exporter.run_export('unused','employee','unused',approval_since=raw)
                self.assertEqual(error.exception.code,'invalid_approval_since')
                query.assert_not_called()

    def test_date_filter_keeps_existing_scope_and_default_ssh_export_unchanged(self):
        model=json.loads((ROOT/'tools/approval_db_columns.json').read_bytes())
        old=exporter.make_queries('employee',model,live_scope=True)
        self.assertEqual(old,exporter.make_queries('employee',model,live_scope=True,approval_since=None))
        filtered=exporter.make_queries("employee' OR 1=1--",model,live_scope=True,approval_since='1700000000')
        self.assertNotIn("employee'",'\n'.join(filtered.values()))
        self.assertTrue(all('1700000000' in sql for sql in filtered.values()))


@unittest.skipUnless(importlib.util.find_spec('pgserver') and sys.platform!='win32','Disposable pgserver runtime required')
class PostgreSQLApprovalDateTests(unittest.TestCase):
    def test_real_sql_exports_new_drafts_only_and_merges_with_unchanged_old_documents(self):
        with tempfile.TemporaryDirectory(prefix='segio-approval-date-') as tmp:
            root=Path(tmp)
            runtime=root/'runtime'
            runtime.mkdir()
            with patch.dict(os.environ,{'XDG_RUNTIME_DIR':str(runtime),'XDG_CACHE_HOME':str(root/'cache')}):
                import pgserver
                with pgserver.get_server(root/'pgdata',cleanup_mode='delete') as server:
                    self.exercise(root,server,pgserver)

    def exercise(self,root,server,pgserver):
        psql=Path(pgserver.__file__).parent/'pginstall/bin/psql'
        model=json.loads((ROOT/'tools/approval_db_columns.json').read_bytes())
        database=None
        def sql(statement):
            result=subprocess.run([str(psql),'-XAt','-w','-d',server.get_uri(database),'-v','ON_ERROR_STOP=1'],
                                  input=statement,text=True,capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,'Disposable SQL fixture failed; raw details omitted')
            return result.stdout
        sql('CREATE DATABASE cell_000;')
        database='cell_000'
        def insert(table,**values):
            literal=lambda value:'NULL' if value is None else "'"+str(value).replace("'","''")+"'"
            return 'INSERT INTO '+table+' ('+','.join('"'+key+'"' for key in values)+') VALUES ('+','.join(literal(v) for v in values.values())+');'
        statements=[]
        for table,columns in model.items():
            def dtype(column):
                return 'integer' if column in ('id','no','p_no') or (column=='doc_no' and table in ('approval_send_box','approval_send_box_backup','approval_favorite')) else 'text'
            statements.append('CREATE TABLE '+table+' ('+','.join('"'+column+'" '+dtype(column) for column in columns)+');')
        statements.extend([
            insert('approval_member_info',no=1,uid='target',rname='Employee'),
            insert('approval_document',no=1,uid='target',wdate=1000000000,title='Old draft original',context='Old body'),
            insert('approval_document',no=5,uid='target',wdate='not a date',title='Unknown draft date',context='Original'),
            insert('approval_document',no=6,uid='target',wdate=None,title='NULL draft date',context='Original'),
            insert('approval_document',no=7,uid='target',wdate=0,title='Zero draft date',context='Original')])
        sql('\n'.join(statements))
        cell=root/'cell'
        (cell/'etc').mkdir(parents=True)
        (cell/'data/approval/upload').mkdir(parents=True)
        (cell/'data/approval/upload/new.txt').write_bytes(b'NEW_APPROVAL_ATTACHMENT')
        config=cell/'etc/db.conf'
        # This is a disposable test DB; no operational settings are used.
        from urllib.parse import urlsplit
        user=urlsplit(server.get_uri()).username
        dbname='cell_000'
        config.write_text('DBMS=pgsql\nHOST='+str(server.get_postmaster_info().socket_dir)+'\nDBNAME='+dbname+'\nUSER='+user+'\nPW=synthetic\n')
        live=root/'live'
        (live/'jobs').mkdir(parents=True)
        worker.write_json(str(live/'config.json'),{'root':str(live),'db_config':str(config),'cell':str(cell),'psql':str(psql)})
        def export(number,since=None,legacy=None):
            job_id='%032x'%number
            job=live/'jobs'/job_id
            job.mkdir()
            request={'username':'target','approvals':True,'mail':False,'created_at':int(time.time())}
            if since is not None:request['approval_since']=str(since)
            worker.write_json(str(job/'request.json'),request)
            if legacy:
                code='import sys;sys.path.insert(0,sys.argv[1]);import live_export_worker as w;w.run_job(sys.argv[2],sys.argv[3])'
                result=subprocess.run([legacy,'-c',code,str(ROOT/'tools'),str(live/'config.json'),job_id],capture_output=True,timeout=120)
                self.assertEqual(result.returncode,0,'Legacy runtime failed; raw output omitted')
            else:worker.run_job(str(live/'config.json'),job_id)
            status=worker.read_json(str(job/'status.json'))
            self.assertEqual(status['state'],'ready',status)
            target=root/('backup-'+str(number))
            target.mkdir()
            unpack_verified(job/'archive.zip',target,'target')
            return target
        original=export(1)
        build_local_catalog(original,'target')
        baseline=DateBaseline(original,'target')
        since=baseline.approval_since
        old_page=(original/'documents/1/document.html').read_bytes()
        old_record=(original/'documents/1/database.json').read_bytes()
        sql('\n'.join([
            "UPDATE approval_document SET cdate='%d',title='Old draft changed later',progress='completed' WHERE no=1;"%(since+1),
            insert('approval_line',no=1,p_no=1,uid='target',wdate=since+1,state='approve'),
            insert('approval_document',no=2,uid='target',wdate=since,title='New draft at boundary',context='New body'),
            insert('approval_document',no=3,uid='other',reference='target@@Employee',wdate=since+1,title='New referenced draft',context='New body'),
            insert('approval_document',no=4,uid='other',wdate=since+1,title='OTHER USER MUST NOT EXPORT',context='Private'),
            insert('approval_document_backup',id=800,no=8,uid='target',wdate=since+1,backupdate=since+2,title='New historical draft',context='History'),
            insert('approval_attach_info',no=2,doc_no='2',fname='new.txt',fpath='new.txt')]))
        before=sql('SELECT count(*) FROM approval_document;')
        delta=export(2,since)
        items=json.loads((delta/'catalog.json').read_bytes())['items']
        self.assertEqual({item['id'] for item in items},{'2','3','8-history-800'})
        self.assertFalse((delta/'documents/1').exists())
        self.assertFalse((delta/'documents/4').exists())
        self.assertEqual((delta/'documents/2/attachments/2.txt').read_bytes(),b'NEW_APPROVAL_ATTACHMENT')
        baseline.validate_approval_delta(json.loads((delta/'manifest.json').read_bytes()))
        baseline.prepare(delta,lambda:None,lambda *a,**k:None,lambda _:None,mail=False)
        build_local_catalog(delta,'target')
        self.assertTrue(verify_archive(delta)['ok'])
        self.assertEqual((delta/'documents/1/database.json').read_bytes(),old_record)
        self.assertEqual((original/'documents/1/document.html').read_bytes(),old_page)
        self.assertIn(b'Old draft original',(delta/'documents/1/document.html').read_bytes())
        self.assertEqual(search(delta,'Old draft original',kind='approval')['total'],1)
        self.assertEqual(search(delta,'Old draft changed later',kind='approval')['total'],0)
        self.assertEqual(search(delta,kind='approval')['total'],7)
        self.assertEqual(sql('SELECT count(*) FROM approval_document;'),before)
        legacy=os.environ.get('SEGIO_TEST_PYTHON26')
        if legacy:
            legacy_copy=export(3,since,legacy)
            self.assertEqual({item['id'] for item in json.loads((legacy_copy/'catalog.json').read_bytes())['items']},
                             {'2','3','8-history-800'})
            self.assertTrue(verify_archive(legacy_copy)['ok'])
