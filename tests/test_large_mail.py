from contextlib import closing
from email.message import Message
import errno
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file, search
from segio_archive.db_client import DBCollector, processing_error, unpack_verified
from segio_archive.mail_import import build_local_catalog
from segio_archive.portable_mail import page_path, prepare_archive, TEXT_LIMIT
from segio_archive.storage import require_space
from segio_archive.web import Config, CollectionError
import test_portable_mail as portable_fixture


class LargePersonalMailTests(unittest.TestCase):
    def personal_fixture(self, root, large=False):
        fixture=portable_fixture.PortableMailTests().fixture(root,large=large)
        (root/'catalog.json').write_text('{"items":[]}')
        source={'name':'inbox.mbox','format':'mbox','path':'mail/raw/000000.mbox',
                **fixture['files']['mail/raw/000000.mbox']}
        (root/'mail/sources.json').write_text(json.dumps({'user':'employee','sources':[source]}))
        fixture.update({'format':'segio-db-html/1','user':'employee','live_export':{},'warnings':[],
                        'coverage':[],'state':'collected_with_warnings','account_deletion_verified':False})
        for name in ('catalog.json','mail/sources.json'):
            fixture['files'][name]=digest_file(root/name)
        (root/'manifest.json').write_text(json.dumps(fixture))
        return fixture

    def test_personal_message_over_32mib_streams_all_attachments_with_bounded_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            original=self.personal_fixture(root,large=True)
            original_open=Path.open
            test=self
            class Bounded:
                def __init__(self,stream):self.stream=stream
                def read(self,size=-1):
                    test.assertTrue(0<=size<=1024*1024,'Unbounded raw message read')
                    return self.stream.read(size)
                def readline(self,size=-1):
                    test.assertTrue(0<=size<=1024*1024,'Unbounded raw message line read')
                    return self.stream.readline(size)
                def __enter__(self):return self
                def __exit__(self,*args):self.stream.close()
                def __getattr__(self,key):return getattr(self.stream,key)
            def bounded(path,*args,**kwargs):
                opened=original_open(path,*args,**kwargs)
                return Bounded(opened) if path==root/'mail/raw/000000.mbox' else opened
            with patch.object(Path,'open',bounded):
                result=build_local_catalog(root,'employee')
            self.assertEqual(result['counts']['mail'],3)
            page=root/page_path('mail-000000002')
            self.assertIn('대용량 첨부가 있는 본문',page.read_text())
            attachment=next((page.parent/'attachments').glob('*large.bin'))
            expected=hashlib.sha256()
            for _ in range(450):expected.update(b'x'*57000)
            self.assertEqual(digest_file(attachment),{'bytes':25650000,'sha256':expected.hexdigest()})
            self.assertEqual(digest_file(root/'mail/raw/000000.mbox'),original['files']['mail/raw/000000.mbox'])
            self.assertEqual(json.loads((root/'catalog.json').read_text())['items'],[])
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                self.assertLess(db.execute("SELECT max(length(body)) FROM items WHERE kind='mail'").fetchone()[0],4096)
            self.assertTrue(verify_archive(root)['ok'])

    def test_body_preview_limit_applies_to_whole_multipart_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.personal_fixture(root)
            source=root/'mail/raw/000000.mbox'
            with source.open('wb') as stream:
                stream.write(b'From synthetic@example.test Tue Oct 20 10:00:00 2026\nSubject: Multiple large text parts\nContent-Type: multipart/mixed; boundary=parts\n\n')
                for number in range(3):
                    stream.write(b'--parts\nContent-Type: text/'+(b'html' if number==2 else b'plain')+b'; charset=utf-8\n\n')
                    for _ in range(96):stream.write(b'x'*65536)
                    stream.write(b'\n')
                stream.write(b'--parts--\n')
            manifest=json.loads((root/'manifest.json').read_text())
            manifest['files']['mail/raw/000000.mbox']=digest_file(source)
            sources={'user':'employee','sources':[{'name':'inbox','path':'mail/raw/000000.mbox',**manifest['files']['mail/raw/000000.mbox']}]}
            (root/'mail/sources.json').write_text(json.dumps(sources))
            manifest['files']['mail/sources.json']=digest_file(root/'mail/sources.json')
            (root/'manifest.json').write_text(json.dumps(manifest))
            build_local_catalog(root,'employee')
            page=root/page_path('mail-000000000')
            self.assertLess(page.stat().st_size,TEXT_LIMIT+16384)
            self.assertIn('x'*1000,page.read_text())
            self.assertIn('전체 내용은 원본 MBOX에 보존',page.read_text())
            self.assertTrue(verify_archive(root)['ok'])

    def test_verified_download_survives_local_disk_failure_and_recovers_without_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            payload=b'From synthetic@example.test Tue Oct 20 10:00:00 2026\nSubject: Recover me\n\noriginal content\n'
            sources={'user':'employee','sources':[{'name':'inbox','path':'mail/raw/000000.bin',
                     'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}]}
            contents={'index.html':b'<p>Import pending</p>','catalog.json':b'{"items":[]}',
                      'mail/sources.json':json.dumps(sources).encode(),'mail/raw/000000.bin':payload}
            manifest={'format':'segio-db-html/1','user':'employee','state':'collected_with_warnings',
                      'account_deletion_verified':False,'counts':{'approval':0},'coverage':[],'warnings':[],
                      'live_export':{'job':'a'*32,'approvals':True,'mail':True},
                      'files':{name:{'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()} for name,body in contents.items()}}
            data=io.BytesIO()
            with zipfile.ZipFile(data,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
                archive.writestr('manifest.json',json.dumps(manifest))
                for name,body in contents.items():archive.writestr(name,body)
            body=data.getvalue()
            incoming=io.BytesIO(body)
            incoming.status=200
            incoming.headers=Message()
            incoming.headers['Content-Type']='application/zip'
            incoming.headers['Content-Length']=str(len(body))
            opener=Mock()
            opener.open.return_value=incoming
            client=DBCollector(Config('https://example.invalid/','employee',root/'output'))
            client.job='a'*32
            client.context,client.page=Mock(),Mock()
            client.user_agent='Synthetic Browser'
            def failed_finalize(stage,archive,destination):
                self.assertFalse(archive.exists(),'Temporary ZIP must be removed before indexing')
                raise OSError(errno.ENOSPC,'Synthetic disk full')
            with patch('segio_archive.db_client.download_opener',return_value=opener),patch.object(client,'finalize_download',side_effect=failed_finalize):
                with self.assertRaisesRegex(CollectionError,'LOCAL_INDEX_PENDING'):
                    client.receive({'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()})
            saved=next((root/'output').glob('segio-employee-partial*'))
            self.assertEqual((saved/'mail/raw/000000.bin').read_bytes(),payload)
            self.assertFalse(list((root/'output').glob('*.zip')))
            self.assertFalse(list((root/'output').glob('.segio-download-*')))
            self.assertEqual(json.loads((saved/'manifest.json').read_text())['local_processing']['state'],'pending')
            prepare_archive(saved)
            self.assertNotIn('local_processing',json.loads((saved/'manifest.json').read_text()))
            self.assertEqual(search(saved,kind='mail')['items'][0]['title'],'Recover me')
            self.assertTrue(verify_archive(saved)['ok'])
            opener.open.assert_called_once()

    def test_zip64_transfer_container_extracts_and_verifies(self):
        import test_db_client as db_fixture
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            with patch.object(zipfile,'ZIP64_LIMIT',256):
                archive=db_fixture.make_archive(root,contents={'index.html':b'<p>ZIP64</p>'*400})
            with zipfile.ZipFile(archive) as bundle:
                self.assertTrue(any(member.extract_version>=45 for member in bundle.infolist()))
            destination=root/'unpacked'
            destination.mkdir()
            unpack_verified(archive,destination,'employee')
            self.assertTrue(verify_archive(destination)['ok'])

    def test_space_and_filesystem_errors_are_specific_for_large_backups(self):
        with patch('segio_archive.storage.shutil.disk_usage',return_value=Mock(free=40*1024**3)):
            with self.assertRaisesRegex(CollectionError,'DISK_FULL'):
                require_space(Path('.'),50*1024**3,'메일 변환')
        for number,code in ((errno.ENOSPC,'DISK_FULL'),(errno.EFBIG,'FILE_TOO_LARGE')):
            result=processing_error(OSError(number,'PRIVATE_DETAIL'),'색인')
            self.assertIn(code,str(result))
            self.assertNotIn('PRIVATE_DETAIL',str(result))


if __name__=='__main__':unittest.main()
