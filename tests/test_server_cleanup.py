import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from segio_archive.db_client import DBCollector
from segio_archive.web import CollectionError, Config


class ServerCleanupClientTests(unittest.TestCase):
    def client(self, root):
        client=DBCollector(Config('https://example.invalid/','employee',root))
        client.job='a'*32
        client.csrf='0'*64
        client.phase='Windows DB·메일 검색 색인 생성'
        return client

    def test_cleanup_refreshes_expired_token_without_starting_export_and_checks_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            calls=[]
            def response(action,**fields):
                calls.append(action)
                if action=='info':return {'csrf':'b'*64}
                self.assertEqual(action,'cleanup')
                self.assertEqual(client.csrf,'b'*64)
                return {'job':client.job,'state':'cleaned','archive_deleted':True,'job_deleted':True}
            with patch.object(client,'live_call',side_effect=response):
                client.cleanup_remote()
                client.cleanup_remote()
            self.assertEqual(calls,['info','cleanup'])
            self.assertTrue(client.cleanup_confirmed)
            self.assertEqual(client.phase,'Windows DB·메일 검색 색인 생성')

    def test_lost_cleanup_response_is_retried_and_missing_or_wrong_proof_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            proof={'job':client.job,'state':'cleaned','archive_deleted':True,'job_deleted':True}
            with patch.object(client,'live_call',side_effect=[{'csrf':'b'*64},TimeoutError(),{'csrf':'c'*64},proof]),patch('segio_archive.db_client.time.sleep'):
                client.cleanup_remote()
            self.assertTrue(client.cleanup_confirmed)
            for bad in ({'state':'cleaned'},dict(proof,job='d'*32),dict(proof,archive_deleted=False)):
                client=self.client(Path(tmp))
                with patch.object(client,'live_call',side_effect=lambda action,**_: {'csrf':'b'*64} if action=='info' else bad) as calls,patch('segio_archive.db_client.time.sleep'):
                    with self.assertRaises(CollectionError):client.cleanup_remote()
                    with self.assertRaises(CollectionError):client.cleanup_remote()
                self.assertEqual(calls.call_count,6)
                self.assertFalse(client.cleanup_confirmed)

    def test_running_job_cancellation_is_not_reported_as_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            result={'job':client.job,'state':'cancellation_requested','archive_deleted':False,'job_deleted':False}
            with patch.object(client,'live_call',side_effect=[{'csrf':'b'*64},result]):client.cleanup_remote()
            self.assertTrue(client.cleanup_cancel_requested)
            self.assertFalse(client.cleanup_confirmed)

    def test_cleanup_failure_keeps_local_backup_and_records_unconfirmed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'manifest.json').write_text('{"files":{},"state":"collected"}')
            (root/'original.bin').write_bytes(b'original')
            client=self.client(root)
            with patch.object(client,'cleanup_remote',side_effect=CollectionError('cleanup_failed')):
                client.finish_remote_cleanup(root)
            manifest=json.loads((root/'manifest.json').read_text())
            self.assertEqual(manifest['server_cleanup'],{'state':'pending','archive_deleted':False})
            self.assertEqual((root/'original.bin').read_bytes(),b'original')


if __name__=='__main__':unittest.main()
