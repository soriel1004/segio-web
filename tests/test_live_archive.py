import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from segio_archive.archive import verify_archive
from segio_archive.db_client import DBCollector
from segio_archive.mail_import import build_local_catalog
from segio_archive.viewer import Viewer
from segio_archive.web import Config, CollectionError
from test_login_bridge import NativePHPFixture, PASSWORD, PHP
from live_fixtures import synthetic_export

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import live_export_worker as worker
import export_approval_db as exporter


def create_fixture(root):
    root=Path(root).resolve()
    (root/'jobs').mkdir(exist_ok=True)
    mail=root/'home/e/employee/mbox'
    mail.mkdir(parents=True,exist_ok=True)
    content=(b'From synthetic@example.test Tue Oct 20 10:00:00 2026\n'
             b'Subject: =?utf-8?b?7ZqM7J2Y?=\r\nFrom: synthetic@example.test\r\n'
             b'To: employee@example.test\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'+
             '회의 본문\r\n'.encode()+b'\nFrom synthetic@example.test Tue Oct 20 11:00:00 2026\n'
             b'Subject: =?euc-kr?b?vcPH6A==?=\nContent-Type: text/html; charset=utf-8\n\n'
             b'<p>mail two</p><script>window.UNSAFE=1</script><img src="https://external.invalid/pixel">\n')
    from email.message import EmailMessage
    message=EmailMessage()
    message['Subject']='첨부 확인'
    message['From']='synthetic@example.test'
    message.set_content('원본 첨부 보존 시험')
    message.add_attachment(b'SYNTHETIC_ATTACHMENT',maintype='application',subtype='octet-stream',filename='../../CON.txt')
    content+=b'\nFrom synthetic@example.test Tue Oct 20 12:00:00 2026\n'+message.as_bytes()
    (mail/'inbox.mbox').write_bytes(content)
    (mail/'native.index').write_bytes(b'\x00SYNTHETIC_NATIVE_INDEX\x00')
    if os.name!='nt':
        descriptor=os.open(os.fsencode(mail)+b'/'+ '한글.idx'.encode('cp949'),os.O_WRONLY|os.O_CREAT,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(b'\x00SYNTHETIC_CP949_INDEX\x00')
    other=root/'home/o/other/mbox'
    other.mkdir(parents=True,exist_ok=True)
    (other/'inbox.mbox').write_bytes(b'From other@example.test Tue Oct 20 12:00:00 2026\nSubject: OTHER_USER_MUST_NOT_EXPORT\n\nprivate\n')
    database=root/'database.json'
    database.write_text(json.dumps({'employee':{'uid':'employee','title':'현재 DB 첫 문서'},
                                    'other':{'uid':'other','title':'OTHER_USER_MUST_NOT_EXPORT'}},ensure_ascii=False),encoding='utf-8')
    settings={'root':str(root),'db_config':str(database),'cell':str(root),
              'mail_template':(root/'home/{initial}/{user}/mbox').as_posix(),
              'csrf_secret':'b'*64,'python':sys.executable,'worker':str(root/'worker.py')}
    (root/'config.json').write_text(json.dumps(settings),encoding='utf-8')
    launcher=f'''import sys
sys.path.insert(0,{str(ROOT/'tools')!r})
sys.path.insert(0,{str(ROOT/'tests')!r})
import live_export_worker as worker
from live_fixtures import synthetic_export
worker.run_export=synthetic_export
raise SystemExit(worker.main())
'''
    (root/'worker.py').write_text(launcher,encoding='utf-8')
    return settings,content


class LiveWorkerTests(unittest.TestCase):
    def test_personal_index_stores_original_date_header_instead_of_backup_date(self):
        from segio_archive.compact_mail import search
        from segio_archive.db_client import unpack_verified
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            mailbox=root/'home/e/employee/mbox/inbox.mbox'
            original=mailbox.read_bytes().replace(b'Subject:',b'Date: Tue, 01 Jan 2002 10:30:00 +0900\nSubject:',1)
            mailbox.write_bytes(original)
            job=root/'jobs'/('e'*32)
            job.mkdir()
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':True,'mail':True,'created_at':1})
            with patch.object(worker,'run_export',side_effect=synthetic_export):
                worker.run_job(str(root/'config.json'),'e'*32)
            self.assertEqual(worker.read_json(str(job/'status.json'))['state'],'ready')
            result=root/'result'
            result.mkdir()
            unpack_verified(job/'archive.zip',result,'employee')
            build_local_catalog(result,'employee')
            items=search(result,kind='mail')['items']
            self.assertEqual(items[0]['date'],'2002-01-01 10:30 +0900')
            self.assertEqual(items[0]['date_raw'],'Tue, 01 Jan 2002 10:30:00 +0900')
            self.assertEqual((result/'mail/raw/000000.mbox').read_bytes(),original)
            self.assertTrue(verify_archive(result)['ok'])

    def test_control_random_response_keeps_plain_cli_compatible_and_requires_os_entropy(self):
        import subprocess
        responses = [json.loads(subprocess.check_output([sys.executable,
            str(ROOT/'tools/live_export_worker.py'), '--random-token', '--bridge-control']))
            for _ in range(2)]
        for value in responses:
            self.assertEqual(value['protocol'], 'segio-worker-control/1')
            self.assertEqual(value['operation'], 'random-token')
            self.assertIs(value['ok'], True)
            self.assertRegex(value['token'], r'\A[0-9a-f]{32}\Z')
        self.assertNotEqual(responses[0]['token'], responses[1]['token'])
        from io import StringIO
        output = StringIO()
        with patch.object(worker.sys, 'argv', ['worker', '--random-token', '--bridge-control']), \
             patch.object(worker.os, 'urandom', side_effect=OSError('unavailable')), \
             patch('sys.stdout', output):
            self.assertEqual(worker.main(), 1)
        self.assertEqual(output.getvalue(), '')

    def test_random_token_helper_uses_os_entropy_without_starting_a_job(self):
        import subprocess
        tokens=[subprocess.check_output([sys.executable, str(ROOT/'tools/live_export_worker.py'), '--random-token'])
                for _ in range(2)]
        for token in tokens:
            self.assertRegex(token, rb'\A[0-9a-f]{32}\r?\n\Z')
        self.assertNotEqual(tokens[0], tokens[1])
        with patch.object(worker.sys, 'argv', ['worker', '--random-token']), \
             patch.object(worker.os, 'urandom', side_effect=OSError('entropy unavailable')), \
             patch.object(worker, 'run_job') as run_job:
            self.assertEqual(worker.main(), 1)
            run_job.assert_not_called()

    def test_worker_records_anonymous_failure_after_cleaning_partial_exports(self):
        import errno
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            for number, exception, code in (
                    (1, worker.SafeError('DO_NOT_LOG_DB_PASSWORD', 'db_permission_denied'), 'db_permission_denied'),
                    (2, OSError(errno.ENOSPC, 'DO_NOT_LOG_MAIL_PATH'), 'server_disk_full')):
                job_id=f'{number:032x}'
                job=root/'jobs'/job_id
                job.mkdir()
                worker.write_json(str(job/'request.json'), {'username':'employee','approvals':True,'mail':True,'created_at':1})
                with patch.object(worker, 'run_export', side_effect=exception):
                    worker.run_job(str(root/'config.json'), job_id)
                status=worker.read_json(str(job/'status.json'))
                self.assertEqual(status['state'], 'failed')
                self.assertEqual(status['phase'], code)
                self.assertEqual(status['diagnostic']['stage'], 'database')
                self.assertFalse((job/'work').exists())
                self.assertFalse((job/'archive.zip').exists())
                shutil.rmtree(job)  # Client cleanup must not erase safe diagnosis.
                text=(root/'jobs/last-failure.json').read_text()
                self.assertEqual(json.loads(text)['phase'], code)
                for secret in ('employee', 'DO_NOT_LOG', str(root), job_id):
                    self.assertNotIn(secret, text)

    def test_diagnostic_checks_schema_without_selecting_records_and_cleans_password_file(self):
        from io import StringIO
        import diagnose_live_archive as diagnostic
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            seen=[]
            def probe(config, folder, sql, binary):
                seen.extend(sql)
                (Path(folder)/'pgpass').write_text('DO_NOT_REPORT_PASSWORD')
                raise worker.SafeError('DO_NOT_REPORT_PASSWORD', 'db_schema_mismatch')
            output=StringIO()
            with patch.object(diagnostic, 'psql_readonly', side_effect=probe), patch('sys.stdout', output):
                self.assertEqual(diagnostic.diagnose(str(root)), 1)
            value=json.loads(output.getvalue())
            self.assertEqual(value['error'], 'db_schema_mismatch')
            self.assertEqual(value['diagnostic']['stage'], 'database')
            self.assertNotIn('DO_NOT_REPORT_PASSWORD', output.getvalue())
            self.assertFalse(list((root/'jobs').glob('.diagnose-*')))
            selects=[sql for sql in seen if sql.startswith('SELECT ')]
            self.assertEqual(len(selects), 14)
            self.assertTrue(all(sql.endswith('WHERE false;') for sql in selects))
            self.assertTrue(any(sql.startswith('EXPLAIN SELECT ') for sql in seen))
            self.assertIn('BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY;', seen)

    def test_unencoded_cp949_rfc822_subject_is_searchable_and_raw_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            raw=(b'Delivered-To: employee@example.test\r\nX-Original-To: employee@example.test\r\nSubject: '+
                 '한글 원문'.encode('cp949')+b'\r\nFrom: synthetic@example.test\r\n\r\nbody\r\n')
            (root/'home/e/employee/mbox/plain.eml').write_bytes(raw)
            job_id='c'*32
            job=root/'jobs'/job_id
            job.mkdir()
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':1})
            worker.run_job(str(root/'config.json'),job_id)
            from segio_archive.db_client import unpack_verified
            result=root/'result'
            result.mkdir()
            unpack_verified(job/'archive.zip',result,'employee')
            manifest=build_local_catalog(result,'employee')
            self.assertEqual(manifest['counts']['mail'],4)
            self.assertEqual((result/'mail/raw/000002.eml').read_bytes(),raw)
            with closing(sqlite3.connect(result/'catalog.sqlite3')) as database:
                self.assertEqual(database.execute('SELECT count(*) FROM items WHERE title=?',('한글 원문',)).fetchone()[0],1)
            self.assertTrue(verify_archive(result)['ok'])

    def test_mail_only_and_changed_mail_does_not_report_completed_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            settings,_=create_fixture(root)
            job_id='d'*32
            job=root/'jobs'/job_id
            job.mkdir()
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':1})
            with patch.object(worker,'run_export') as database:
                worker.run_job(str(root/'config.json'),job_id)
                database.assert_not_called()
            self.assertEqual(worker.read_json(str(job/'status.json'))['state'],'ready')
            target=root/'changed'
            target.mkdir()
            calls=0
            def changed():
                nonlocal calls
                calls+=1
                if calls==3:
                    with (root/'home/e/employee/mbox/inbox.mbox').open('ab') as stream:
                        stream.write(b'new incoming mail')
            with self.assertRaisesRegex(worker.SafeError,'mail_changed_retry'):
                worker.copy_mail(settings['mail_template'],'employee',str(target),{'files':{},'counts':{},'coverage':[]},changed)

    def test_current_database_and_whole_user_mail_are_exported_without_other_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            settings,original=create_fixture(root)
            for number in range(2):
                job_id=f'{number+1:032x}'
                job=root/'jobs'/job_id
                job.mkdir()
                worker.write_json(str(job/'request.json'),{'username':'employee','approvals':True,'mail':True,'created_at':number})
                records=json.loads((root/'database.json').read_text(encoding='utf-8'))
                records['employee']['title']=f'현재 DB 버전 {number}'
                (root/'database.json').write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8')
                with patch.object(worker,'run_export',side_effect=synthetic_export):
                    worker.run_job(str(root/'config.json'),job_id)
                status=worker.read_json(str(job/'status.json'))
                self.assertEqual(status['state'],'ready',status)
                import zipfile
                folder=root/f'result{number}'
                from segio_archive.db_client import unpack_verified
                folder.mkdir()
                unpack_verified(job/'archive.zip',folder,'employee')
                result=build_local_catalog(folder,'employee')
                self.assertEqual(result['counts']['mail'],3)
                self.assertEqual((folder/'mail/raw/000000.mbox').read_bytes(),original)
                self.assertEqual((root/'home/e/employee/mbox/inbox.mbox').read_bytes(),original)
                self.assertTrue(verify_archive(folder)['ok'])
                with closing(sqlite3.connect(folder/'catalog.sqlite3')) as connection:
                    self.assertEqual(connection.execute("SELECT title FROM items WHERE kind='approval'").fetchone()[0],f'현재 DB 버전 {number}')
                    titles=[r[0] for r in connection.execute("SELECT title FROM items WHERE kind='mail'")]
                    self.assertIn('회의',titles)
                    self.assertIn('시험',titles)
                self.assertEqual((folder/'mail/raw/000001.bin').read_bytes(),b'\x00SYNTHETIC_NATIVE_INDEX\x00')
                if os.name!='nt':
                    sources=json.loads((folder/'mail/sources.json').read_text(encoding='utf-8'))['sources']
                    legacy=next(source for source in sources if source['name']=='한글.idx')
                    self.assertEqual(legacy['name_bytes_hex'],'한글.idx'.encode('cp949').hex())
                    self.assertEqual((folder/legacy['path']).read_bytes(),b'\x00SYNTHETIC_CP949_INDEX\x00')
                self.assertEqual(next((folder/'mail/messages/00000002/attachments').iterdir()).read_bytes(),b'SYNTHETIC_ATTACHMENT')
                for file in folder.rglob('*'):
                    if file.is_file():
                        self.assertNotIn(b'OTHER_USER_MUST_NOT_EXPORT',file.read_bytes())
                page=(folder/'mail/messages/00000001/document.html').read_text(encoding='utf-8')
                self.assertNotIn('window.UNSAFE',page)
                self.assertNotIn('https://external',page)


    def test_mail_paths_and_live_database_scope_reject_unsafe_entitlements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            settings,_=create_fixture(root)
            self.assertEqual(worker.mail_path(settings['mail_template'],'employee'),str(root.resolve()/'home/e/employee/mbox'))
            with patch.object(worker.sys,'version_info',(2,6,9)):
                native=worker.mail_path(settings['mail_template'],'employee')
                self.assertIsInstance(native,bytes)
                self.assertEqual(os.fsdecode(native),str(root.resolve()/'home/e/employee/mbox'))
            for user in ('.','..','../other','guest'):
                with self.assertRaises(worker.SafeError):
                    worker.mail_path(settings['mail_template'],user)
            with self.assertRaises(worker.SafeError):
                worker.mail_path(str(root/'home'),'employee')
            if sys.platform!='win32':
                (root/'home/e/employee/mbox/escape').symlink_to(root/'database.json')
                with self.assertRaises(worker.SafeError):
                    worker.inventory(str(root/'home/e/employee/mbox'))
        sql,_=exporter.selection_sql('employee',live_scope=True)
        self.assertNotIn('approval_favorite',sql)
        self.assertNotIn('approval_comment_info',sql)
        self.assertIn('approval_line',sql)
        # Windows ctime is creation time, unlike the POSIX change marker.
        # Different stat/fstat representations must not reject unchanged mail.
        from types import SimpleNamespace
        before=SimpleNamespace(st_dev=1,st_ino=2,st_size=30,st_mtime=4,st_mtime_ns=4000000000,st_ctime=5,st_ctime_ns=5000000000)
        opened=SimpleNamespace(**{**vars(before),'st_ctime':5.01,'st_ctime_ns':5010000000})
        with patch.object(worker.os,'name','nt'):
            self.assertEqual(worker.file_identity(before),worker.file_identity(opened))
        with patch.object(worker.os,'name','posix'):
            self.assertNotEqual(worker.file_identity(before),worker.file_identity(opened))


@unittest.skipIf(sys.platform=='win32', 'POSIX one-time installer')
class LiveInstallationTests(unittest.TestCase):
    def test_installation_never_prepares_employee_data_or_copies_db_password(self):
        import types
        import install_live_archive as installer
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cell=root/'cell000'
            (cell/'folder/html/segio').mkdir(parents=True)
            (cell/'etc').mkdir()
            config=cell/'etc/db.conf'
            secret=b'SYNTHETIC_DB_PASSWORD_NEVER_COPY'
            config.write_bytes(secret)
            account=types.SimpleNamespace(pw_uid=1234,pw_gid=os.getgid())
            private=cell/'etc/segio-live-archive'
            with patch.object(installer.os,'geteuid',return_value=0), \
                 patch.object(installer.os,'chown') as ownership, \
                 patch.object(installer.pwd,'getpwnam',return_value=account), \
                 patch.object(worker,'run_export') as export:
                installer.install('synthetic-web',str(config))
                export.assert_not_called()
                ownership.assert_any_call(str(private/'jobs'),1234,os.getgid())
            self.assertEqual(config.read_bytes(),secret)
            self.assertEqual(list((private/'jobs').iterdir()),[])
            self.assertEqual((private/'jobs').stat().st_mode&0o777,0o700)
            self.assertEqual((private/'code').stat().st_mode&0o777,0o750)
            settings=json.loads((private/'config.json').read_text(encoding='utf-8'))
            self.assertEqual(settings['mail_template'],str(cell/'home/{initial}/{user}/mbox'))
            self.assertEqual(len(settings['csrf_secret']),64)
            for path in private.rglob('*'):
                if path.is_file():
                    self.assertNotIn(secret,path.read_bytes())
                    self.assertEqual(path.stat().st_mode&0o777,0o640)
            endpoint=cell/'folder/html/segio/archive/live.php'
            self.assertNotIn('__PRIVATE_ROOT__',endpoint.read_text(encoding='utf-8'))
            self.assertEqual(endpoint.stat().st_mode&0o777,0o644)


@unittest.skipUnless(PHP,'PHP CLI required')
class LiveBridgeTests(NativePHPFixture, unittest.TestCase):
    """Real native-auth fixture, detached worker and streaming client."""

    def test_personal_download_keeps_folder_and_never_repackages_a_local_zip(self):
        import zipfile
        original_zip = zipfile.ZipFile
        def read_only_zip(path, mode='r', *args, **kwargs):
            self.assertEqual(mode,'r','Personal downloads must not create an additional local ZIP')
            return original_zip(path, mode, *args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(self.url+'/', 'employee', Path(tmp), mail=True, pdf=False,
                allow_http=True, headless=True, timeout_ms=10000,
                browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
            with patch('segio_archive.db_client.zipfile.ZipFile',side_effect=read_only_zip):
                result=DBCollector(config).run(PASSWORD)
            self.assertTrue(verify_archive(result)['ok'])
            self.assertTrue((result/'index.html').is_file())
            self.assertEqual(list(Path(tmp).glob('*.zip')),[])
            self.assertFalse(list(Path(tmp).glob('.segio-download-*')))
            self.assertEqual((result/'mail/raw/000000.mbox').read_bytes(),self.original)

    def unavailable_child_status(self, text):
        text = text.replace('proc_get_status($process)', 'test_unavailable_status($process)')
        text = text.replace('proc_close($process)', 'test_unavailable_close($process)')
        return text.replace('function new_job_id(', '''
function test_unavailable_status($process) {
    $status=proc_get_status($process);
    if (!$status['running']) $status['exitcode']=-1;
    return $status;
}
function test_unavailable_close($process) { proc_close($process); return -1; }
function new_job_id(''')

    def test_missing_both_exit_statuses_uses_explicit_ack_for_random_and_detach(self):
        bridge = self.root/'live.php'
        original = bridge.read_text()
        text = self.unavailable_child_status(original)
        text = text.replace("function_exists('random_bytes')", 'false')
        text = text.replace("@fopen('/dev/urandom','rb')", "@fopen(__DIR__.'/missing-random','rb')")
        text = text.replace("function_exists('openssl_random_pseudo_bytes')", 'false')
        try:
            bridge.write_text(text)
            opener = self.logged_in()
            with opener.open(self.url+'/segio/archive/live.php?action=diagnostic', timeout=5) as response:
                payload = response.read()
            value = json.loads(payload)
            self.assertEqual(value['worker']['state'], 'acknowledged_exit_unavailable')
            self.assertEqual(value['worker']['exit_code'], -1)
            self.assertTrue(value['worker']['acknowledged'])
            self.assertFalse(value['worker']['exit_status_confirmed'])
            self.assertNotIn(b'"token"', payload)
            with tempfile.TemporaryDirectory() as tmp:
                config = Config(self.url+'/', 'employee', Path(tmp), mail=True, pdf=False,
                    allow_http=True, headless=True, timeout_ms=10000,
                    browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
                result = DBCollector(config).run(PASSWORD)
                self.assertTrue(verify_archive(result)['ok'])
                self.assertEqual((result/'mail/raw/000000.mbox').read_bytes(), self.original)
        finally:
            bridge.write_text(original)

    def test_unknown_exit_rejects_plain_tokens_bad_ack_and_stderr(self):
        bridge, launcher = self.root/'live.php', self.live_root/'worker.py'
        original_bridge, original_worker = bridge.read_text(), launcher.read_bytes()
        good = {'protocol': 'segio-worker-control/1', 'operation': 'random-token', 'ok': True, 'token': 'f'*32}
        cases = [('plain', 'f'*32, ''),
                 ('bad_protocol', json.dumps(dict(good, protocol='unknown')), ''),
                 ('bad_operation', json.dumps(dict(good, operation='detach')), ''),
                 ('bad_token', json.dumps(dict(good, token='bad')), ''),
                 ('error', json.dumps(dict(good, ok=False)), ''),
                 ('stderr', json.dumps(good), 'PRIVATE_RUNTIME_STDERR')]
        try:
            bridge.write_text(self.unavailable_child_status(original_bridge))
            opener = self.logged_in()
            for label, output, stderr in cases:
                with self.subTest(label=label):
                    launcher.write_text('import sys\nprint(%r)\nsys.stderr.write(%r)\n' % (output, stderr))
                    with opener.open(self.url+'/segio/archive/live.php?action=diagnostic', timeout=5) as response:
                        payload = response.read()
                    value = json.loads(payload)
                    self.assertEqual(value['worker']['state'], 'exit_status_unavailable')
                    self.assertFalse(value['worker']['acknowledged'])
                    self.assertNotIn(b'PRIVATE_RUNTIME_STDERR', payload)
                    self.assertNotIn(b'f'*32, payload)
        finally:
            bridge.write_text(original_bridge)
            launcher.write_bytes(original_worker)

    def test_known_failed_exit_is_rejected_even_with_a_valid_control_response(self):
        launcher = self.live_root/'worker.py'
        original = launcher.read_bytes()
        try:
            ack = {'protocol': 'segio-worker-control/1', 'operation': 'random-token', 'ok': True, 'token': 'f'*32}
            launcher.write_text('print(%r)\nraise SystemExit(2)\n' % json.dumps(ack))
            opener = self.logged_in()
            with opener.open(self.url+'/segio/archive/live.php?action=diagnostic', timeout=5) as response:
                value = json.load(response)
            self.assertEqual(value['worker']['state'], 'worker_exit_failed')
            self.assertEqual(value['worker']['exit_code'], 2)
            self.assertFalse(value['worker']['acknowledged'])
            self.assertTrue(value['worker']['exit_status_confirmed'])
        finally:
            launcher.write_bytes(original)

    def test_detach_ack_must_match_the_requested_job(self):
        launcher = self.live_root/'worker.py'
        original = launcher.read_text()
        before = set((self.live_root/'jobs').iterdir())
        replacement = '''
if '--detach' in sys.argv:
    import json
    print(json.dumps({'protocol':'segio-worker-control/1','operation':'detach','ok':True,'job':'WRONG_JOB'}))
    raise SystemExit(0)
raise SystemExit(worker.main())
'''
        try:
            launcher.write_text(original.replace('raise SystemExit(worker.main())', replacement))
            with tempfile.TemporaryDirectory() as tmp:
                config = Config(self.url+'/', 'employee', Path(tmp), mail=True, pdf=False,
                    allow_http=True, headless=True, timeout_ms=10000,
                    browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
                with self.assertRaises(CollectionError) as caught:
                    DBCollector(config).run(PASSWORD)
                self.assertEqual(str(caught.exception), DBCollector.error_message('export_process_unavailable'))
                failure = json.loads((self.live_root/'jobs/last-failure.json').read_text())
                self.assertEqual(failure['phase'], 'export_process_unavailable')
                self.assertEqual(failure['diagnostic']['runtime']['state'], 'invalid_acknowledgement')
                jobs = [p for p in (self.live_root/'jobs').iterdir() if p.is_dir() and p not in before]
                self.assertEqual(len(jobs), 1)
                self.assertEqual(json.loads((jobs[0]/'status.json').read_text())['state'], 'failed')
                self.assertEqual(list(Path(tmp).iterdir()), [])
        finally:
            launcher.write_text(original)
            for job in (self.live_root/'jobs').iterdir():
                if job.is_dir() and job not in before:
                    shutil.rmtree(job)

    def test_cached_process_status_survives_proc_close_minus_one(self):
        bridge = self.root/'live.php'
        original = bridge.read_text()
        text = original.replace("function_exists('random_bytes')", 'false')
        text = text.replace("@fopen('/dev/urandom','rb')", "@fopen(__DIR__.'/missing-random','rb')")
        text = text.replace("function_exists('openssl_random_pseudo_bytes')", 'false')
        text = text.replace('proc_close($process)', 'test_lost_close_status($process)')
        text = text.replace('function new_job_id(',
            'function test_lost_close_status($process) { proc_close($process); return -1; }\nfunction new_job_id(')
        try:
            bridge.write_text(text)
            with tempfile.TemporaryDirectory() as tmp:
                config = Config(self.url+'/', 'employee', Path(tmp), mail=True, pdf=False,
                    allow_http=True, headless=True, timeout_ms=10000,
                    browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
                result = DBCollector(config).run(PASSWORD)
                self.assertTrue(verify_archive(result)['ok'])
                self.assertEqual((result/'mail/raw/000000.mbox').read_bytes(), self.original)
        finally:
            bridge.write_text(original)

    def test_php_runtime_diagnostic_requires_login_and_hides_tokens_and_stderr(self):
        import urllib.error
        from test_login_bridge import local_opener
        url = self.url+'/segio/archive/live.php?action=diagnostic'
        with self.assertRaises(urllib.error.HTTPError) as caught:
            local_opener().open(url, timeout=5)
        with caught.exception as response:
            self.assertEqual(response.code, 401)
            value = json.load(response)
            self.assertEqual(value['protocol'], 'segio-live-archive/1')
            self.assertEqual(value['error'], 'native_login_required')
        opener = self.logged_in()
        launcher = self.live_root/'worker.py'
        original = launcher.read_bytes()
        try:
            # Even a valid-looking token must not turn a failed process into success.
            token = 'f'*32
            launcher.write_text("import sys\nprint(%r)\nsys.stderr.write('PRIVATE_RUNTIME_DETAIL')\nraise SystemExit(2)\n" % token)
            with opener.open(url, timeout=5) as response:
                body = response.read()
            value = json.loads(body)
            self.assertEqual(value['bridge_revision'], 'php-runtime-3')
            self.assertEqual(value['worker']['state'], 'worker_exit_failed')
            self.assertEqual(value['worker']['exit_code'], 2)
            self.assertTrue(value['worker']['stderr_present'])
            for secret in (token.encode(), b'PRIVATE_RUNTIME_DETAIL', PASSWORD.encode(), str(self.live_root).encode()):
                self.assertNotIn(secret, body)
            self.assertFalse([p for p in (self.live_root/'jobs').iterdir() if p.is_dir()])
        finally:
            launcher.write_bytes(original)

    def test_php_runtime_diagnostic_bounds_a_stalled_worker(self):
        bridge, launcher = self.root/'live.php', self.live_root/'worker.py'
        original_bridge, original_worker = bridge.read_text(), launcher.read_bytes()
        try:
            bridge.write_text(original_bridge.replace('microtime(true)+15.0', 'microtime(true)+0.1'))
            launcher.write_text('import time\ntime.sleep(1)\n')
            opener = self.logged_in()
            with opener.open(self.url+'/segio/archive/live.php?action=diagnostic', timeout=5) as response:
                value = json.load(response)
            self.assertEqual(value['worker']['state'], 'timeout')
        finally:
            bridge.write_text(original_bridge)
            launcher.write_bytes(original_worker)

    def test_php_runtime_diagnostic_can_poll_windows_blocking_pipes(self):
        bridge = self.root/'live.php'
        original = bridge.read_text()
        text = original.replace('$stdout_ready=@stream_set_blocking($pipes[1],false);', '$stdout_ready=false;')
        text = text.replace('$stderr_ready=@stream_set_blocking($pipes[2],false);', '$stderr_ready=false;')
        guard = "if (!$nonblocking && DIRECTORY_SEPARATOR!=='\\\\')"
        self.assertIn(guard, text)
        text = text.replace(guard, 'if (false)')
        try:
            bridge.write_text(text)
            opener = self.logged_in()
            with opener.open(self.url+'/segio/archive/live.php?action=diagnostic', timeout=5) as response:
                value = json.load(response)
            self.assertEqual(value['worker']['state'], 'ok')
            self.assertEqual(value['worker']['exit_code'], 0)
            self.assertTrue(value['worker']['acknowledged'])
            self.assertTrue(value['worker']['exit_status_confirmed'])
            expected = json.dumps({'protocol':'segio-worker-control/1', 'operation':'random-token',
                                   'ok':True, 'token':'0'*32}, separators=(',', ':'))
            self.assertEqual(value['worker']['output_bytes'],
                             len(expected.encode('ascii')) + len(os.linesep.encode('ascii')))
        finally:
            bridge.write_text(original)

    def test_native_php_without_random_sources_uses_worker_entropy_and_finishes_export(self):
        bridge=self.root/'live.php'
        original=bridge.read_bytes()
        try:
            text=original.decode().replace("function_exists('random_bytes')", 'false')
            text=text.replace("@fopen('/dev/urandom','rb')", "@fopen(__DIR__.'/missing-random','rb')")
            text=text.replace("function_exists('openssl_random_pseudo_bytes')", 'false')
            bridge.write_text(text,encoding='utf-8')
            with tempfile.TemporaryDirectory() as tmp:
                config=Config(self.url+'/', 'employee', Path(tmp),mail=True,pdf=False,
                              allow_http=True,headless=True,timeout_ms=10000,
                              browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
                result=DBCollector(config).run(PASSWORD)
                self.assertTrue(verify_archive(result)['ok'])
                self.assertEqual((result/'mail/raw/000000.mbox').read_bytes(),self.original)
        finally:
            bridge.write_bytes(original)

    def test_start_failure_before_job_creation_records_original_safe_error_code(self):
        bridge=self.root/'live.php'
        launcher=self.live_root/'worker.py'
        original_bridge,original_worker=bridge.read_bytes(),launcher.read_bytes()
        try:
            text=original_bridge.decode().replace("function_exists('random_bytes')", 'false')
            text=text.replace("@fopen('/dev/urandom','rb')", "@fopen(__DIR__.'/missing-random','rb')")
            text=text.replace("function_exists('openssl_random_pseudo_bytes')", 'false')
            bridge.write_text(text,encoding='utf-8')
            launcher.write_text("print('PRIVATE_WORKER_OUTPUT_MUST_NOT_BE_LOGGED')\nraise SystemExit(1)\n",encoding='utf-8')
            with tempfile.TemporaryDirectory() as tmp:
                config=Config(self.url+'/', 'employee', Path(tmp),mail=True,pdf=False,
                              allow_http=True,headless=True,timeout_ms=10000,
                              browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
                with self.assertRaisesRegex(CollectionError, 'secure_random_unavailable') as caught:
                    DBCollector(config).run(PASSWORD)
                self.assertNotIn('PRIVATE_WORKER_OUTPUT',str(caught.exception))
                self.assertEqual(list(Path(tmp).iterdir()),[])
            saved=(self.live_root/'jobs/last-failure.json').read_bytes()
            self.assertEqual(json.loads(saved)['phase'],'secure_random_unavailable')
            self.assertEqual(json.loads(saved)['diagnostic']['stage'],'server_request')
            self.assertFalse([p for p in (self.live_root/'jobs').iterdir() if p.is_dir()])
            for secret in (b'PRIVATE_WORKER_OUTPUT',b'employee',str(self.live_root).encode()):
                self.assertNotIn(secret,saved)
        finally:
            bridge.write_bytes(original_bridge)
            launcher.write_bytes(original_worker)

    def test_server_database_failure_survives_client_cleanup_without_secrets(self):
        launcher=self.live_root/'worker.py'
        original=launcher.read_bytes()
        try:
            text=original.decode()
            text=text.replace('worker.run_export=synthetic_export',
                "def fail(*args,**kwargs):\n    raise worker.SafeError('PRIVATE_DB_PASSWORD', 'db_schema_mismatch')\nworker.run_export=fail")
            launcher.write_text(text,encoding='utf-8')
            with tempfile.TemporaryDirectory() as tmp:
                config=Config(self.url+'/', 'employee', Path(tmp),mail=True,pdf=False,
                              allow_http=True,headless=True,timeout_ms=10000,
                              browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
                with self.assertRaisesRegex(CollectionError, '서버 결재 DB 추출.*db_schema_mismatch') as caught:
                    DBCollector(config).run(PASSWORD)
                self.assertNotIn('PRIVATE_DB_PASSWORD',str(caught.exception))
                self.assertEqual(list(Path(tmp).iterdir()),[])
            self.assertFalse([p for p in (self.live_root/'jobs').iterdir() if p.is_dir()])
            saved=(self.live_root/'jobs/last-failure.json').read_bytes()
            self.assertEqual(json.loads(saved)['phase'],'db_schema_mismatch')
            self.assertNotIn(b'PRIVATE_DB_PASSWORD',saved)
            self.assertNotIn(b'employee',saved)
        finally:
            launcher.write_bytes(original)
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.live_root=cls.root/'live'
        cls.live_root.mkdir()
        cls.settings,cls.original=create_fixture(cls.live_root)
        php=(ROOT/'tools/live_archive_bridge.php').read_text(encoding='utf-8')
        literal=cls.live_root.as_posix().replace("'","\\'")
        (cls.root/'live.php').write_text(php.replace('__PRIVATE_ROOT__',literal),encoding='utf-8')
        router=(cls.root/'router.php').read_text(encoding='utf-8')
        legacy=r'''
if ($path==='/legacy-popup') {
    echo '<html><body><button onclick="window.open(\'/legacy-login-dynamic\',\'login\',\'width=350,height=280\')">Open login</button></body></html>';
    return;
}
if ($path==='/legacy-login' || $path==='/legacy-login-dynamic') {
    $dynamic=$path==='/legacy-login-dynamic';
    echo '<html><body><form method="post" target="native_zero" action="'.($dynamic?'/before-submit':'/login-legacy').'" '.
         ($dynamic?'onsubmit="this.action=\'/login-legacy\'"':'').'><input name="UID">'.
         '<input name="PASSWD" type="password"><input type="hidden" name="native_hidden" value="preserved">'.
         '<button type="submit">Login</button></form><iframe name="native_zero"></iframe></body></html>';
    return;
}
if ($path==='/login-legacy' && $_SERVER['REQUEST_METHOD']==='POST') {
    if (!isset($_POST['native_hidden']) || $_POST['native_hidden']!=='preserved') {
        header('HTTP/1.1 400 Bad Request'); echo 'Missing native hidden field'; return;
    }
    $_POST['uid']=isset($_POST['UID'])?$_POST['UID']:'';
    $_POST['password']=isset($_POST['PASSWD'])?$_POST['PASSWD']:'';
    $path='/login';
}
'''
        router=router.replace("$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);",
                              "$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);"+legacy)
        router=router.replace("$path==='/segio/archive/bridge.php'","($path==='/segio/archive/bridge.php' || $path==='/segio/archive/live.php')")
        router=router.replace("include __DIR__.'/bridge.php';","include __DIR__.($path==='/segio/archive/live.php'?'/live.php':'/bridge.php');")
        (cls.root/'router.php').write_text(router,encoding='utf-8')

    def test_automatic_uppercase_segio_fields_and_hidden_frame_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Config(self.url+'/legacy-login','employee',Path(tmp),mail=False,pdf=False,
                          allow_http=True,headless=True,timeout_ms=5000,
                          browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
            result=DBCollector(config).run(PASSWORD)
            self.assertTrue(verify_archive(result)['ok'])

    def test_manual_login_waits_for_native_identity_with_password_form_still_present(self):
        for user in ('employee','other'):
            with self.subTest(user=user),tempfile.TemporaryDirectory() as tmp:
                output=Path(tmp)/'output'
                config=Config(self.url+'/legacy-login-dynamic','employee',output,mail=False,pdf=False,
                              allow_http=True,headless=True,manual_login=True,timeout_ms=5000,
                              browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
                observed=[]
                def log(message):
                    if '열린 브라우저에서 로그인하세요' in message:
                        client.page.locator('[name=UID]').fill(user)
                        client.page.locator('[name=PASSWD]').fill(PASSWORD)
                        client.page.evaluate('setTimeout(()=>document.querySelector("form").requestSubmit(),250)')
                client=DBCollector(config,log)
                finalize=client.finalize_download
                def capture(*args):
                    observed.append(client.page.locator('input[type=password]').count())
                    finalize(*args)
                with patch.object(client,'finalize_download',side_effect=capture):
                    if user=='employee':
                        result=client.run('')
                        self.assertTrue(verify_archive(result)['ok'])
                        self.assertEqual(observed,[1])
                    else:
                        with self.assertRaisesRegex(CollectionError,'실제 로그인 계정'):
                            client.run('')
                        self.assertFalse(output.exists())

    def test_service_config_error_reports_safe_checks_without_setting_values(self):
        config=self.live_root/'config.json'
        original=config.read_bytes()
        try:
            config.write_bytes(b'{')
            opener=self.logged_in()
            import urllib.error
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(self.url+'/segio/archive/live.php?action=info')
            with caught.exception as response:
                self.assertEqual(response.code,503)
                body=response.read()
                value=json.loads(body)
            self.assertEqual(value['error'],'service_not_configured')
            self.assertTrue(value['checks']['config_readable'])
            self.assertFalse(value['checks']['config_json_object'])
            self.assertNotIn(str(self.live_root).encode(),body)
            self.assertNotIn(b'csrf_secret',body)
        finally:
            config.write_bytes(original)

    def test_manual_popup_can_close_after_native_login_without_ending_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Config(self.url+'/legacy-popup','employee',Path(tmp),mail=False,pdf=False,
                          allow_http=True,headless=True,manual_login=True,timeout_ms=5000,
                          browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
            popups=[]
            def log(message):
                if '열린 브라우저에서 로그인하세요' in message:
                    with client.page.expect_popup() as opened:
                        client.page.locator('button').click()
                    popup=opened.value
                    popups.append(popup)
                    popup.locator('[name=UID]').fill('employee')
                    popup.locator('[name=PASSWD]').fill(PASSWORD)
                    popup.evaluate('setTimeout(()=>{document.querySelector("iframe").addEventListener("load",()=>window.close(),{once:true});document.querySelector("form").requestSubmit()},250)')
            client=DBCollector(config,log)
            finalize=client.finalize_download
            def capture(*args):
                # Assert before run() closes the whole browser in its finally.
                self.assertTrue(popups[0].is_closed())
                finalize(*args)
            with patch.object(client,'finalize_download',side_effect=capture):
                result=client.run('')
            self.assertTrue(verify_archive(result)['ok'])
            self.assertEqual(len(popups),1)
            self.assertTrue(popups[0].is_closed())

    def test_live_login_db_mail_download_and_subject_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Config(self.url+'/', 'employee', Path(tmp),mail=True,pdf=False,
                          allow_http=True,headless=True,timeout_ms=10000,
                          browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
            path=DBCollector(config).run(PASSWORD)
            self.assertTrue(verify_archive(path)['ok'])
            self.assertEqual((path/'mail/raw/000000.mbox').read_bytes(),self.original)
            self.assertFalse([p for p in (self.live_root/'jobs').iterdir() if p.is_dir()])
            original_manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
            records=json.loads((self.live_root/'database.json').read_text(encoding='utf-8'))
            records['employee']['title']='요청 후 변경된 최신 DB'
            (self.live_root/'database.json').write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8')
            second=DBCollector(config).run(PASSWORD)
            latest=json.loads((second/'manifest.json').read_text(encoding='utf-8'))
            self.assertNotEqual(original_manifest['live_export']['job'],latest['live_export']['job'])
            self.assertEqual(second.name,'segio-employee-2')
            with closing(sqlite3.connect(second/'catalog.sqlite3')) as database:
                self.assertEqual(database.execute("SELECT title FROM items WHERE kind='approval'").fetchone()[0],'요청 후 변경된 최신 DB')
            viewer=Viewer(path)
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=config.browser_executable)
                    context=browser.new_context()
                    requests=[]
                    context.on('request',lambda request:requests.append(request.url))
                    page=context.new_page()
                    page.goto(viewer.url)
                    page.locator('#tab-mail').click()
                    page.locator('#q').fill('회의')
                    page.locator('#search-button').click()
                    from playwright.sync_api import expect
                    expect(page.locator('article')).to_have_count(1)
                    self.assertIn('회의',page.locator('article h2').inner_text())
                    self.assertTrue(all(url.startswith(viewer.url.rsplit('/',1)[0]) for url in requests))
                    browser.close()
            finally:
                viewer.close()

    def test_start_requires_csrf_and_rejects_other_account_job_access(self):
        opener=self.logged_in()
        import urllib.error
        import urllib.parse
        endpoint=self.url+'/segio/archive/live.php'
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(endpoint+'?action=start',data=b'approvals=1&mail=1')
        self.assertEqual(caught.exception.code,403)
        job_id='f'*32
        job=self.live_root/'jobs'/job_id
        job.mkdir(exist_ok=True)
        worker.write_json(str(job/'request.json'),{'username':'employee','created_at':0})
        other=self.logged_in('other')
        for action in ('status','download'):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                other.open(endpoint+f'?action={action}&job={job_id}')
            self.assertEqual(caught.exception.code,404)
        shutil.rmtree(job)


if __name__=='__main__':
    unittest.main()
