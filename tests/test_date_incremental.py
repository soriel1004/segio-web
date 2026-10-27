import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import search
from segio_archive.date_incremental import DateBaseline, update_lock
from segio_archive.db_client import unpack_verified
from segio_archive.mail_import import build_local_catalog
from segio_archive.web import Cancelled, CollectionError
from test_live_archive import create_fixture, worker, ROOT
from live_fixtures import synthetic_export


class DateIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        create_fixture(self.root)
        self.mail = self.root/'home/e/employee/mbox'
        self.old = self.export(1)
        build_local_catalog(self.old, 'employee')
        self.base = DateBaseline(self.old, 'employee')
        self.old_inodes = {entry[2][:2] for entry in worker.inventory(str(self.mail))}

    def export(self, number, base=None, changed=(), approvals=True, mail=True):
        identifier = '%032x' % number
        job = self.root/'jobs'/identifier
        job.mkdir()
        request = {'username':'employee','approvals':approvals,'mail':mail,'created_at':int(time.time())}
        if base:
            if mail:
                request['mail_since'] = str(base.since)
            if approvals and base.approval_since is not None:
                request['approval_since'] = str(base.approval_since)
        worker.write_json(str(job/'request.json'),request)
        changed_inodes = {worker.fingerprint(str(self.mail/name))[:2] for name in changed}
        def timestamp(identity):
            return base.since-10 if identity[:2] in self.old_inodes and identity[:2] not in changed_inodes else base.since+1
        original = worker.copy_mail_file
        copied = []
        def copy(path, *args):
            copied.append(os.fsdecode(os.path.basename(path)))
            return original(path, *args)
        with patch.object(worker,'run_export',side_effect=synthetic_export), patch.object(worker,'copy_mail_file',side_effect=copy):
            if base:
                with patch.object(worker,'mail_file_time',side_effect=timestamp):
                    worker.run_job(str(self.root/'config.json'),identifier)
            else:
                worker.run_job(str(self.root/'config.json'),identifier)
        self.copied = copied
        status = worker.read_json(str(job/'status.json'))
        self.assertEqual(status['state'],'ready',status)
        target = self.root/('backup-'+str(number))
        target.mkdir()
        unpack_verified(job/'archive.zip',target,'employee')
        return target

    def snapshot(self):
        return {p.relative_to(self.old).as_posix():p.read_bytes() for p in self.old.rglob('*') if p.is_file()}

    def prepare(self, target, base=None):
        base = base or self.base
        base.validate_delta(target)
        base.validate_approval_delta(json.loads((target/'manifest.json').read_bytes()))
        base.prepare(target,lambda:None,lambda *a,**k:None,lambda _:None)
        build_local_catalog(target,'employee')
        self.assertTrue(verify_archive(target)['ok'])

    def test_only_new_file_is_read_indexed_rendered_and_merged_into_same_backup(self):
        from segio_archive import compact_mail, portable_mail
        page = self.old/portable_mail.page_path('mail-000000000')
        original_page = (page.stat().st_ino,page.stat().st_mtime_ns,page.read_bytes())
        before = self.snapshot()
        (self.mail/'reply.eml').write_bytes(b'Subject: Reply to old thread\nDate: Mon, 01 Jan 1990 10:00:00 +0900\n\nnew body')
        target = self.export(2,self.base)
        self.assertEqual(self.copied,['reply.eml'])
        with patch.object(compact_mail,'read_headers',wraps=compact_mail.read_headers) as headers, patch.object(portable_mail,'render_record',wraps=portable_mail.render_record) as render:
            self.prepare(target)
            self.assertEqual(headers.call_count,1)
            self.assertEqual(render.call_count,1)
        self.assertEqual(before,self.snapshot())
        result = self.base.commit(target,lambda:None,lambda _:None)
        self.assertEqual(result,self.old)
        self.assertFalse(target.exists())
        self.assertTrue(verify_archive(result)['ok'])
        self.assertEqual(search(result,kind='mail')['total'],4)
        self.assertEqual(search(result,'Reply to old',kind='mail')['total'],1)
        self.assertEqual(original_page,(page.stat().st_ino,page.stat().st_mtime_ns,page.read_bytes()))
        self.assertEqual(search(result,'Reply to old',kind='mail')['items'][0]['date_raw'],'Mon, 01 Jan 1990 10:00:00 +0900')

    def test_preflight_keeps_selected_output_and_names_missing_required_files_before_login(self):
        from segio_archive.backup_folders import MissingBackupFile
        from segio_archive.db_client import DBCollector
        from segio_archive.web import Config
        output=self.root/'Windows selected'
        config=Config('https://example.invalid/','employee',output,incremental_base=self.old)
        client=DBCollector(config)
        client.prepare_baseline()
        self.assertEqual(config.output,output)
        self.assertEqual(client.working_output,self.old.parent)
        self.assertEqual(client.diagnostic_output,output)
        self.assertFalse(output.exists())
        raw=self.base.sources[0]['path']
        for name,key in (('manifest.json','manifest'),('catalog.json','catalog'),
                         ('catalog.sqlite3','search_database'),('mail/sources.json','mail_sources'),(raw,'mail_raw')):
            with self.subTest(missing=name):
                path=self.old/name
                payload=path.read_bytes()
                path.unlink()
                before=self.snapshot()
                try:
                    client=DBCollector(config)
                    with patch('segio_archive.db_client.launch_browser') as browser:
                        with self.assertRaisesRegex(MissingBackupFile,name):client.run('PRIVATE_PASSWORD')
                    browser.assert_not_called()
                    self.assertFalse(client.start_requested)
                    self.assertEqual(before,self.snapshot())
                    record=json.loads((output/'last-client-failure.json').read_bytes())
                    self.assertEqual(record['baseline'],{'required_file':key})
                    self.assertEqual(record['error'],'BASELINE_FILE_MISSING')
                    self.assertEqual(record['phase'],'이전 백업 확인')
                    self.assertNotIn('PRIVATE_PASSWORD',json.dumps(record))
                finally:path.write_bytes(payload)
        (output/'last-client-failure.json').unlink()
        self.assertIsInstance(DateBaseline(self.old,'employee'),DateBaseline)

    def test_no_new_mail_does_not_read_or_render_old_payloads(self):
        from segio_archive import portable_mail
        target = self.export(2,self.base)
        self.assertEqual(self.copied,[])
        opened=Path.open
        def no_old_raw(path,*args,**kwargs):
            if '/mail/raw/' in path.as_posix():
                raise AssertionError('Old raw mail must not be opened')
            return opened(path,*args,**kwargs)
        with patch.object(portable_mail,'render_record',side_effect=AssertionError('Old mail must not be rendered')), patch.object(Path,'open',no_old_raw):
            self.base.validate_delta(target)
            self.base.prepare(target,lambda:None,lambda *a,**k:None,lambda _:None)
            build_local_catalog(target,'employee')
        self.assertTrue(verify_archive(target)['ok'])
        self.assertEqual(search(target,kind='mail')['total'],3)
        self.assertEqual(json.loads((target/'manifest.json').read_bytes())['last_incremental']['added_mail_files'],0)

    def test_overlap_deduplicates_same_file_and_next_backup_retains_old_ids(self):
        (self.mail/'reply.eml').write_bytes(b'Subject: New reply\n\nnew')
        target = self.export(2,self.base)
        self.prepare(target)
        self.base.commit(target,lambda:None,lambda _:None)
        base = DateBaseline(self.old,'employee')
        target = self.export(3,base)
        self.prepare(target,base)
        self.assertEqual(search(target,'New reply',kind='mail')['total'],1)
        self.assertEqual(json.loads((target/'manifest.json').read_bytes())['last_incremental']['added_mail_files'],0)

    def test_changed_mailbox_is_replaced_without_touching_old_backup_until_commit(self):
        before = self.snapshot()
        with (self.mail/'inbox.mbox').open('ab') as output:
            output.write(b'\nFrom reply@example.test Tue Oct 20 13:00:00 2026\nSubject: Appended reply\n\nnew\n')
        target = self.export(2,self.base,changed=['inbox.mbox'])
        self.assertEqual(self.copied,['inbox.mbox'])
        self.prepare(target)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(search(target,kind='mail')['total'],4)
        self.assertEqual(search(target,'Appended reply',kind='mail')['total'],1)
        self.assertFalse((target/'mail/offline/mail-000000000').exists())
        self.assertTrue((self.old/'mail/offline/mail-000000000/index.html').exists())

    def test_cancel_and_failed_swap_leave_previous_backup_byte_identical(self):
        before = self.snapshot()
        target = self.export(2,self.base)
        self.prepare(target)
        def cancel():raise Cancelled('cancelled')
        with self.assertRaises(Cancelled):self.base.commit(target,cancel,lambda _:None)
        self.assertEqual(before,self.snapshot())
        replace = os.replace
        def locked(source,destination):
            if Path(source)==target:raise PermissionError('LOCKED')
            return replace(source,destination)
        with patch('segio_archive.date_incremental.os.replace',side_effect=locked):
            with self.assertRaises(PermissionError):self.base.commit(target,lambda:None,lambda _:None)
        self.assertEqual(before,self.snapshot())
        self.assertTrue(target.exists())
        self.assertFalse(self.base.journal(self.old).exists())

    def test_interrupted_directory_exchange_is_recovered_without_deleting_staged_data(self):
        before = self.snapshot()
        previous = self.old.parent/('.segio-previous-'+'a'*32)
        journal = self.base.journal(self.old)
        journal.write_text(json.dumps({'target':self.old.name,'previous':previous.name}))
        os.replace(self.old,previous)
        DateBaseline(self.old,'employee')
        self.assertEqual(before,self.snapshot())
        self.assertFalse(journal.exists())

    def test_old_server_deleted_mail_and_old_draft_remain_as_previously_backed_up(self):
        (self.mail/'inbox.mbox').unlink()
        records=json.loads((self.root/'database.json').read_bytes())
        records['employee']['title']='Updated old approval'
        (self.root/'database.json').write_text(json.dumps(records))
        target=self.export(2,self.base)
        self.prepare(target)
        self.assertEqual(search(target,kind='mail')['total'],3)
        self.assertEqual(search(target,'Updated old approval',kind='approval')['total'],0)
        self.assertEqual(search(target,'현재 DB 첫 문서',kind='approval')['total'],1)

    def test_only_new_drafts_merge_and_old_draft_status_changes_are_not_fetched(self):
        cutoff=self.base.approval_since
        records=json.loads((self.root/'database.json').read_bytes())
        records['employee']={'documents':[
            {'no':'1','title':'Old draft completed later','wdate':cutoff-100,'cdate':cutoff+1,'progress':'완료'},
            {'no':'2','title':'New draft at boundary','wdate':cutoff},
            {'no':'3','title':'New draft after backup','wdate':cutoff+1},
            {'no':'4','title':'Unknown draft date','wdate':'unknown'}]}
        (self.root/'database.json').write_text(json.dumps(records))
        target=self.export(2,self.base)
        received=json.loads((target/'catalog.json').read_bytes())['items']
        self.assertEqual([item['id'] for item in received],['2','3'])
        self.assertFalse((target/'documents/1').exists())
        self.prepare(target)
        self.assertEqual(search(target,kind='approval')['total'],3)
        self.assertEqual(search(target,'현재 DB 첫 문서',kind='approval')['total'],1)
        self.assertEqual(search(target,'Old draft completed later',kind='approval')['total'],0)
        self.assertEqual(search(target,'New draft',kind='approval')['total'],2)
        self.assertTrue(verify_archive(target)['ok'])
        manifest=json.loads((target/'manifest.json').read_bytes())
        self.assertEqual(manifest['approval_delta']['basis'],'document_wdate')
        self.assertEqual(manifest['last_incremental']['received_approval_documents'],2)

    def test_mail_only_update_keeps_approval_clock_and_legacy_baselines_have_safe_fallbacks(self):
        target=self.export(2,self.base,approvals=False)
        self.assertNotIn('approval_checkpoint',json.loads((target/'manifest.json').read_bytes()))
        self.base.prepare(target,lambda:None,lambda *a,**k:None,lambda _:None,approvals=False)
        manifest=json.loads((target/'manifest.json').read_bytes())
        self.assertEqual(manifest['approval_checkpoint'],self.base.approval_since)
        path=self.old/'manifest.json'
        legacy=json.loads(path.read_bytes())
        legacy.pop('approval_checkpoint')
        path.write_text(json.dumps(legacy))
        self.assertEqual(DateBaseline(self.old,'employee').approval_since,legacy['live_export']['requested_at'])
        legacy['live_export']['approvals']=False
        path.write_text(json.dumps(legacy))
        self.assertIsNone(DateBaseline(self.old,'employee').approval_since)

    def test_approval_only_update_keeps_the_previous_mail_clock_and_raw_mail(self):
        records=json.loads((self.root/'database.json').read_bytes())
        records['employee']={'no':'2','title':'New approval without mail','wdate':self.base.approval_since}
        (self.root/'database.json').write_text(json.dumps(records))
        target=self.export(2,self.base,mail=False)
        self.base.validate_approval_delta(json.loads((target/'manifest.json').read_bytes()))
        self.base.prepare(target,lambda:None,lambda *a,**k:None,lambda _:None,mail=False)
        build_local_catalog(target,'employee')
        manifest=json.loads((target/'manifest.json').read_bytes())
        self.assertEqual(manifest['mail_checkpoint'],self.base.since)
        self.assertEqual(search(target,kind='mail')['total'],3)
        self.assertEqual(search(target,kind='approval')['total'],2)
        self.assertTrue(verify_archive(target)['ok'])

    def test_approval_delta_requires_exact_since_basis_revision_and_clock(self):
        target=self.export(2,self.base)
        manifest=json.loads((target/'manifest.json').read_bytes())
        self.base.validate_approval_delta(manifest)
        import copy
        for key,value in (('since',self.base.approval_since+1),('basis','completion_date'),
                          ('revision','other'),('protocol','other')):
            invalid=copy.deepcopy(manifest)
            invalid['approval_delta'][key]=value
            with self.assertRaises(CollectionError):self.base.validate_approval_delta(invalid)
        for value in (None,0,True,self.base.approval_since-1):
            invalid=copy.deepcopy(manifest)
            invalid['approval_checkpoint']=value
            with self.assertRaises(CollectionError):self.base.validate_approval_delta(invalid)

    def test_file_timestamp_selection_is_inclusive_and_does_not_use_date_header(self):
        identity=(1,2,100,1000.0,1500.0)
        self.assertEqual(worker.mail_file_time(identity),1500)
        self.assertEqual(worker.mail_file_time((1,2,100,1000000000000000,1500000000000000)),1500000)
        for raw in ('-1','0','1.2','true','9'*20,str(int(time.time())+1000),1,True,{}):
            with self.assertRaises(worker.SafeError):worker.parse_mail_since(raw)
        self.assertEqual(worker.parse_mail_since('123'),123)
        target=self.export(2,self.base)
        manifest=json.loads((target/'manifest.json').read_bytes())
        manifest['mail_delta']['since']+=1
        (target/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(CollectionError):self.base.validate_delta(target)
        with self.assertRaises(CollectionError):DateBaseline(self.old,'other')

    def test_concurrent_commit_is_rejected_and_mail_only_keeps_approval_and_checkpoint(self):
        with update_lock(self.old):
            with self.assertRaises(CollectionError):DateBaseline(self.old,'employee')
        target=self.export(2,self.base)
        self.base.prepare(target,lambda:None,lambda *a,**k:None,lambda _:None,approvals=False)
        build_local_catalog(target,'employee')
        self.assertEqual(search(target,kind='approval')['total'],1)
        self.assertTrue(verify_archive(target)['ok'])

    def test_non_link_filesystems_remain_safe_with_verified_private_copies(self):
        import errno
        before=self.snapshot()
        target=self.export(2,self.base)
        with patch('segio_archive.date_incremental.os.link',side_effect=OSError(errno.EXDEV,'NO_LINKS')):
            self.prepare(target)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(search(target,kind='mail')['total'],3)

    def test_quarantined_attachment_is_not_restored_by_incremental_backup(self):
        from segio_archive import portable_mail
        manifest=json.loads((self.old/'manifest.json').read_bytes())
        attachment=next(name for name in manifest['files'] if '/attachments/' in name)
        (self.old/attachment).unlink()
        before=self.snapshot()
        target=self.export(2,self.base)
        write=portable_mail.write_generated
        def no_restore(root,name,*args):
            if name==attachment:raise AssertionError('Deleted attachment was restored')
            return write(root,name,*args)
        with patch.object(portable_mail,'write_generated',side_effect=no_restore):
            self.prepare(target)
        self.assertEqual(before,self.snapshot())
        result=json.loads((target/'manifest.json').read_bytes())
        self.assertEqual(result['portable_mail']['unavailable_attachment_count'],1)
        self.assertNotIn(attachment,result['files'])

    @unittest.skipUnless(os.environ.get('SEGIO_TEST_PYTHON26'),'Real Python 2.6 is required')
    def test_real_python26_date_delta_reads_only_recent_native_files(self):
        # Real ctime cannot be backdated with utime; cross the next whole second.
        cutoff=int(time.time())+1
        time.sleep(max(0,cutoff-time.time()+.05))
        (self.mail/'legacy-reply.eml').write_bytes(b'Subject: Legacy old-dated new reply\nDate: Mon, 01 Jan 1990 00:00:00 +0900\n\nnew')
        identifier='f'*32
        job=self.root/'jobs'/identifier
        job.mkdir()
        worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':int(time.time()),'mail_since':str(cutoff)})
        result=subprocess.run([os.environ['SEGIO_TEST_PYTHON26'],str(ROOT/'tools/live_export_worker.py'),'--config',str(self.root/'config.json'),'--job',identifier],capture_output=True,timeout=30,env={**os.environ,'LC_ALL':'C','LANG':'C'})
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertEqual(worker.read_json(str(job/'status.json'))['state'],'ready')
        with zipfile.ZipFile(job/'archive.zip') as archive:
            sources=json.loads(archive.read('mail/sources.json'))['sources']
            self.assertEqual([item['name'] for item in sources],['legacy-reply.eml'])
            self.assertEqual(len([name for name in archive.namelist() if name.startswith('mail/raw/')]),1)
