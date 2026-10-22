import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import zipfile

from segio_archive.archive import verify_archive
from segio_archive.db_client import unpack_verified
from segio_archive.incremental import Baseline
from segio_archive.mail_import import build_local_catalog
from segio_archive.web import CollectionError
from test_live_archive import create_fixture, worker
from live_fixtures import synthetic_export


class IncrementalTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('SEGIO_TEST_PYTHON26'),'Real Python 2.6 is required')
    def test_legacy_python26_worker_omits_unchanged_mail_from_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            _,old=self.export(root,1)
            build_local_catalog(old,'employee')
            base=Baseline(old,'employee')
            job_id='c'*32
            job=root/'jobs'/job_id
            job.mkdir()
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':1,
                                                     'mail_baseline':base.payload()})
            source=Path(__file__).resolve().parents[1]/'tools/live_export_worker.py'
            result=subprocess.run([os.environ['SEGIO_TEST_PYTHON26'],str(source),'--config',str(root/'config.json'),'--job',job_id],
                                  capture_output=True,timeout=30,env={**os.environ,'LC_ALL':'C','LANG':'C'})
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            status=worker.read_json(str(job/'status.json'))
            self.assertEqual(status['state'],'ready',status)
            with zipfile.ZipFile(job/'archive.zip') as archive:
                manifest=json.loads(archive.read('manifest.json'))
                self.assertEqual(len(manifest['incremental']['reused_mail']),len(base.entries))
                self.assertFalse(any(name.startswith('mail/raw/') for name in archive.namelist()))
    def test_confirmed_cancel_removes_job_after_worker_stops_and_preserves_originals_and_other_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            _,original_mail=create_fixture(root)
            job_id='a'*32
            job=root/'jobs'/job_id
            job.mkdir()
            other=root/'jobs'/('b'*32)
            other.mkdir()
            (other/'archive.zip').write_bytes(b'OTHER_JOB')
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':1})
            write=worker.PackingProgress.write
            def cancel(instance,data):
                (job/'discard-on-cancel').touch()
                (job/'cancel').touch()
                return write(instance,data)
            with patch.object(worker.PackingProgress,'write',cancel):worker.run_job(str(root/'config.json'),job_id)
            self.assertFalse(job.exists())
            self.assertEqual(worker.read_json(str(root/'jobs'/('.'+job_id+'.cleanup.json')))['state'],'cleaned')
            self.assertEqual((other/'archive.zip').read_bytes(),b'OTHER_JOB')
            self.assertEqual((root/'home/e/employee/mbox/inbox.mbox').read_bytes(),original_mail)
    def export(self,root,number,baseline=None):
        job_id=('%032x'%number)
        job=root/'jobs'/job_id
        job.mkdir()
        request={'username':'employee','approvals':True,'mail':True,'created_at':1}
        if baseline:request['mail_baseline']=baseline.payload()
        worker.write_json(str(job/'request.json'),request)
        with patch.object(worker,'run_export',side_effect=synthetic_export):
            worker.run_job(str(root/'config.json'),job_id)
        self.assertEqual(worker.read_json(str(job/'status.json'))['state'],'ready')
        target=root/('backup-'+str(number))
        target.mkdir()
        unpack_verified(job/'archive.zip',target,'employee')
        return job,target

    def test_unchanged_mail_is_omitted_from_zip_and_new_snapshot_remains_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            mail=root/'home/e/employee/mbox'
            (mail/'gone.eml').write_bytes(b'Subject: Later deleted\n\nold body')
            _,old=self.export(root,1)
            build_local_catalog(old,'employee')
            original={str(path.relative_to(old)):path.read_bytes() for path in old.rglob('*') if path.is_file()}
            base=Baseline(old,'employee')
            (mail/'gone.eml').unlink()
            (mail/'new.eml').write_bytes(b'Subject: Late arrival\nDate: Mon, 01 Jan 1990 10:00:00 +0900\n\nnew old-dated mail')
            (mail/'native.index').write_bytes(b'CHANGED_NATIVE_INDEX')
            records=json.loads((root/'database.json').read_bytes())
            records['employee']['title']='Changed old approval'
            (root/'database.json').write_text(json.dumps(records))
            job,new=self.export(root,2,base)
            pending=json.loads((new/'manifest.json').read_bytes())
            reused=pending['incremental']['reused_mail']
            self.assertGreaterEqual(len(reused),1)
            with zipfile.ZipFile(job/'archive.zip') as archive:
                self.assertTrue(set(reused).isdisjoint(archive.namelist()))
            events=[]
            count,size=base.restore(new,lambda:None,lambda *args,**kwargs:events.append(args))
            self.assertEqual(count,len(reused))
            self.assertGreater(size,0)
            build_local_catalog(new,'employee')
            self.assertTrue(verify_archive(new)['ok'])
            from segio_archive.compact_mail import search
            self.assertEqual(search(new,'Late arrival',kind='mail')['total'],1)
            self.assertEqual(search(new,'Later deleted',kind='mail')['total'],0)
            self.assertEqual(search(new,'Changed old approval',kind='approval')['total'],1)
            self.assertEqual({str(p.relative_to(old)):p.read_bytes() for p in old.rglob('*') if p.is_file()},original)
            sources=json.loads((new/'mail/sources.json').read_bytes())['sources']
            source=next(item for item in sources if item['name']=='inbox.mbox')
            self.assertNotEqual((new/source['path']).stat().st_ino,(old/base.entries[source['name_bytes_hex']]['path']).stat().st_ino)
            # A changed MBOX is sent in full, including an old Date header.
            with (mail/'inbox.mbox').open('ab') as stream:
                stream.write(b'\nFrom late@example.test\nSubject: Appended later\nDate: Mon, 01 Jan 1990 10:00:00 +0900\n\nlate\n')
            base2=Baseline(new,'employee')
            _,third=self.export(root,3,base2)
            base2.restore(third,lambda:None,lambda *args,**kwargs:None)
            build_local_catalog(third,'employee')
            self.assertEqual(search(third,'Appended later',kind='mail')['total'],1)
            self.assertTrue(verify_archive(third)['ok'])

    def test_wrong_user_incomplete_backup_and_tampered_reused_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            _,old=self.export(root,1)
            build_local_catalog(old,'employee')
            with self.assertRaises(CollectionError):Baseline(old,'other')
            manifest_path=old/'manifest.json'
            original=manifest_path.read_bytes()
            manifest=json.loads(original)
            manifest['local_processing']={'state':'pending'}
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(CollectionError):Baseline(old,'employee')
            manifest_path.write_bytes(original)
            base=Baseline(old,'employee')
            _,new=self.export(root,2,base)
            relative=next(item['path'] for item in base.entries.values() if item['path'].endswith('.mbox'))
            path=old/relative
            payload=path.read_bytes()
            path.write_bytes(b'X'+payload[1:])
            with self.assertRaisesRegex(CollectionError,'SHA-256'):
                base.restore(new,lambda:None,lambda *args,**kwargs:None)
            self.assertEqual(path.read_bytes(),b'X'+payload[1:])
            self.assertEqual(manifest_path.read_bytes(),original)

    def test_server_baseline_is_bounded_and_rejects_duplicates_and_invalid_hashes(self):
        for value in ('x',[],json.dumps({'protocol':'wrong','files':[]}),
                      json.dumps({'protocol':'segio-incremental/1','files':[{'name_bytes_hex':'aa','bytes':1,'sha256':'no'}]})):
            with self.subTest(value=repr(value)[:60]):
                with self.assertRaisesRegex(worker.SafeError,'invalid_baseline'):worker.parse_mail_baseline(value)
        entry={'name_bytes_hex':'aa','bytes':50*1024**3,'sha256':'a'*64}
        value={'protocol':'segio-incremental/1','files':[entry]}
        self.assertEqual(worker.parse_mail_baseline(json.dumps(value))['aa']['bytes'],50*1024**3)
        value['files'].append(entry)
        with self.assertRaises(worker.SafeError):worker.parse_mail_baseline(json.dumps(value))
