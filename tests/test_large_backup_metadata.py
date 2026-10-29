"""Completed large mail backups must remain valid incremental baselines."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import test_date_incremental as fixtures
from test_db_client import make_archive
from segio_archive.archive import verify_archive
from segio_archive.backup_folders import (BaselineValidationError, backup_candidates,
                                         read_backup_metadata)
from segio_archive.client_diagnostics import write_failure
from segio_archive.compact_mail import digest_file, search
from segio_archive.date_incremental import DateBaseline, LOOKBACK_SECONDS
from segio_archive.db_client import DBCollector, unpack_verified
from segio_archive.web import Cancelled, CollectionError, Config
from test_live_archive import worker, ROOT

REPORTED_MANIFEST_BYTES=33612110


def pad_json(path, size=REPORTED_MANIFEST_BYTES):
    # Legal JSON whitespace reproduces the reported size without constructing
    # tens of thousands of irrelevant mail files in this regression fixture.
    with path.open('ab') as stream:
        remaining=size-stream.tell()
        while remaining>0:
            block=b' '*min(1024*1024,remaining)
            stream.write(block)
            remaining-=len(block)


class LargeBackupMetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DateIncrementalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.old=self.fixture.old

    def test_reported_32mb_completed_manifest_is_discovered_and_incremented_in_place(self):
        path=self.old/'manifest.json'
        pad_json(path)
        signature=digest_file(path)
        self.assertEqual(signature['bytes'],REPORTED_MANIFEST_BYTES)
        self.assertIn(self.old,backup_candidates(self.old.parent,'employee'))
        client=DBCollector(Config('https://example.invalid/','employee',self.fixture.root/'output',
                                  incremental_base=self.old))
        with patch('segio_archive.db_client.launch_browser') as browser:
            client.prepare_baseline()
        browser.assert_not_called()
        baseline=client.baseline
        self.assertEqual(baseline.since,baseline.mail_checkpoint-LOOKBACK_SECONDS)
        self.assertEqual(baseline.approval_since,baseline.approval_checkpoint-LOOKBACK_SECONDS)
        self.assertEqual(digest_file(path),signature)
        (self.fixture.mail/'new.eml').write_bytes(b'Subject: Next day arrival\n\nnew')
        target=self.fixture.export(2,baseline)
        self.fixture.prepare(target,baseline)
        self.assertEqual(digest_file(path),signature)
        self.assertEqual(baseline.commit(target,lambda:None,lambda _:None),self.old)
        self.assertEqual(search(self.old,'Next day arrival',kind='mail')['total'],1)
        self.assertTrue(verify_archive(self.old)['ok'])
        self.assertIsInstance(DateBaseline(self.old,'employee'),DateBaseline)

    def test_large_old_and_received_sources_keep_hash_validation_and_merge(self):
        sources=self.old/'mail/sources.json'
        pad_json(sources)
        manifest_path=self.old/'manifest.json'
        manifest=json.loads(manifest_path.read_bytes())
        manifest['files']['mail/sources.json']=digest_file(sources)
        manifest_path.write_text(json.dumps(manifest))
        baseline=DateBaseline(self.old,'employee')
        before=digest_file(sources)
        target=self.fixture.export(2,baseline)
        received=target/'mail/sources.json'
        pad_json(received)
        manifest_path=target/'manifest.json'
        manifest=json.loads(manifest_path.read_bytes())
        manifest['files']['mail/sources.json']=digest_file(received)
        manifest_path.write_text(json.dumps(manifest))
        self.fixture.prepare(target,baseline)
        self.assertEqual(digest_file(sources),before)
        self.assertEqual(search(target,kind='mail')['total'],3)
        self.assertTrue(verify_archive(target)['ok'])

    def test_zip_transfer_accepts_large_manifest_and_still_verifies_all_assets(self):
        original=make_archive(self.fixture.root)
        large=self.fixture.root/'large-metadata.zip'
        with zipfile.ZipFile(original) as old,zipfile.ZipFile(large,'w',zipfile.ZIP_DEFLATED) as new:
            for name in old.namelist():
                if name!='manifest.json':
                    new.writestr(name,old.read(name))
                    continue
                with new.open(name,'w') as stream:
                    data=old.read(name)
                    stream.write(data)
                    remaining=REPORTED_MANIFEST_BYTES-len(data)
                    while remaining:
                        block=b' '*min(1024*1024,remaining)
                        stream.write(block)
                        remaining-=len(block)
        target=self.fixture.root/'unpacked-large'
        target.mkdir()
        unpack_verified(large,target,'employee')
        self.assertEqual((target/'manifest.json').stat().st_size,REPORTED_MANIFEST_BYTES)
        self.assertTrue(verify_archive(target)['ok'])

    def test_metadata_is_not_rejected_based_only_on_reported_file_size(self):
        path=self.fixture.root/'metadata.json'
        path.write_bytes(b'{"valid":true}')
        with patch.object(Path,'stat',return_value=SimpleNamespace(st_size=1024**4)),patch.object(Path,'is_symlink',return_value=False):
            value,signature=read_backup_metadata(path)
        self.assertEqual(value,{'valid':True})
        self.assertEqual(signature,digest_file(path))

    def test_server_generated_metadata_has_no_fixed_size_cap_but_job_requests_remain_bounded(self):
        path=self.fixture.root/'large-server-manifest.json'
        path.write_text('{"title":"서버 검증 목록"}',encoding='utf-8')
        pad_json(path)
        self.assertEqual(worker.read_json(str(path),None),{'title':'서버 검증 목록'})
        with self.assertRaises(worker.SafeError):
            worker.read_json(str(path))
        legacy=os.environ.get('SEGIO_TEST_PYTHON26')
        if legacy:
            script=('import sys;sys.path.insert(0,sys.argv[1]);import live_export_worker as w;'
                    'v=w.read_json(sys.argv[2],None);assert len(v)==1;'
                    'assert isinstance(v["title"],unicode)')
            result=subprocess.run([legacy,'-c',script,str(ROOT/'tools'),str(path)],capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,'Legacy metadata read failed; raw output omitted')

    def test_large_generated_approval_manifest_finishes_the_live_worker_job(self):
        export=fixtures.synthetic_export
        def large_export(*args,**kwargs):
            folder,archive=export(*args,**kwargs)
            pad_json(Path(folder)/'manifest.json')
            return folder,archive
        with patch.object(fixtures,'synthetic_export',side_effect=large_export):
            target=self.fixture.export(2,self.fixture.base)
        self.fixture.prepare(target)
        self.assertTrue(verify_archive(target)['ok'])

    def test_manifest_failures_distinguish_json_format_origin_and_required_files(self):
        path=self.old/'manifest.json'
        original=path.read_bytes()
        for reason,field,value in (('manifest_format_unsupported','format','segio-archive/1'),
                                   ('live_export_missing','live_export',None),
                                   ('manifest_files_invalid','files',None)):
            with self.subTest(reason=reason):
                manifest=json.loads(original)
                manifest[field]=value
                path.write_text(json.dumps(manifest))
                before=digest_file(path)
                with self.assertRaises(BaselineValidationError) as caught:
                    DateBaseline(self.old,'employee')
                self.assertEqual(caught.exception.baseline_diagnostic,{'reason':reason})
                self.assertEqual(digest_file(path),before)
        path.write_bytes(b'{\n"user": "PRIVATE_EMPLOYEE",\n"broken": ')
        with self.assertRaises(BaselineValidationError) as caught:
            DateBaseline(self.old,'employee')
        record=write_failure(self.fixture.root/'diagnostics',caught.exception,'이전 백업 확인')
        value=json.loads(record.read_bytes())['baseline']
        self.assertEqual(value['reason'],'manifest_json_invalid')
        self.assertEqual(value['metadata_bytes'],path.stat().st_size)
        self.assertEqual(value['json_line'],3)
        self.assertGreater(value['json_column'],0)
        self.assertNotIn('PRIVATE',record.read_text())

    def test_large_metadata_cancel_closes_file_without_modifying_previous_backup(self):
        path=self.old/'manifest.json'
        pad_json(path)
        signature=digest_file(path)
        calls=0
        def checkpoint():
            nonlocal calls
            calls+=1
            if calls==3:
                raise Cancelled('Cancelled metadata read')
        with self.assertRaises(Cancelled):
            read_backup_metadata(path,checkpoint)
        renamed=path.with_name('manifest-cancelled.json')
        path.rename(renamed)
        renamed.rename(path)
        self.assertEqual(digest_file(path),signature)

    def test_same_size_manifest_change_prevents_commit_and_keeps_both_folders(self):
        path=self.old/'manifest.json'
        with path.open('ab') as stream:
            stream.write(b' ')
        baseline=DateBaseline(self.old,'employee')
        target=self.fixture.export(2,baseline)
        self.fixture.prepare(target,baseline)
        with path.open('r+b') as stream:
            stream.seek(-1,2)
            stream.write(b'\n')
        self.assertEqual(path.stat().st_size,baseline.original_manifest_signature['bytes'])
        with self.assertRaisesRegex(CollectionError,'기존 백업이 변경'):
            baseline.commit(target,lambda:None,lambda _:None)
        self.assertTrue(self.old.is_dir())
        self.assertTrue(target.is_dir())
        self.assertTrue(verify_archive(self.old)['ok'])

    def test_invalid_encoding_is_reported_without_private_json_content(self):
        path=self.old/'manifest.json'
        path.write_bytes(b'{"secret":"PRIVATE_VALUE",\xff}')
        with self.assertRaises(BaselineValidationError) as caught:
            read_backup_metadata(path)
        record=write_failure(self.fixture.root/'diagnostics',caught.exception,'이전 백업 확인')
        value=json.loads(record.read_bytes())['baseline']
        self.assertEqual(value['reason'],'manifest_json_invalid')
        self.assertEqual(value['metadata_bytes'],path.stat().st_size)
        self.assertNotIn('PRIVATE',record.read_text())
