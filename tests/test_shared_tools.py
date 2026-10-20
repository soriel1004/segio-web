"""One tools bundle must serve both existing SSH and employee-login clients."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import check_shared_tools as shared
from segio_archive.ssh_client import TOOLS


class SharedBundleTests(unittest.TestCase):
    def test_common_bundle_keeps_the_existing_ssh_upload_contract(self):
        contents = shared.load_bundle(str(ROOT / 'tools'))
        self.assertEqual(set(TOOLS), set(shared.SSH_FILES))
        self.assertTrue(set(shared.WEB_CODE_FILES).issubset(contents))

    def test_missing_or_invalid_files_fail_with_only_a_fixed_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / 'tools'
            shutil.copytree(ROOT / 'tools', tools, ignore=shutil.ignore_patterns('__pycache__'))
            (tools / 'remote_ssh_export.py').unlink()
            with self.assertRaisesRegex(shared.BundleError, 'remote_ssh_export.py'):
                shared.load_bundle(str(tools))
            (tools / 'remote_ssh_export.py').write_text('PRIVATE_SECRET = !!! invalid')
            with self.assertRaises(shared.BundleError) as caught:
                shared.load_bundle(str(tools))
            self.assertNotIn('PRIVATE_SECRET', str(caught.exception))


@unittest.skipIf(sys.platform == 'win32', 'POSIX server installation')
class SharedInstallationTests(unittest.TestCase):
    def install(self, cell, root, template=None, tools=None):
        import install_live_archive as installer
        account = types.SimpleNamespace(pw_uid=1234, pw_gid=os.getgid())

        def private_directory(path, gid):
            # Emulate only root ownership; real mkdir/modes and all writes run.
            Path(path).mkdir(exist_ok=True, mode=0o750)
            os.chmod(path, 0o750)

        with patch.object(installer.os, 'geteuid', return_value=0), \
             patch.object(installer.os, 'chown'), \
             patch.object(installer.pwd, 'getpwnam', return_value=account), \
             patch.object(installer, 'private_directory', side_effect=private_directory), \
             patch.object(installer, 'HERE', str(tools or ROOT / 'tools')):
            installer.install('synthetic-web', str(cell / 'etc/db.conf'), str(root), template)

    def fixture(self, root):
        cell = root / 'cell000'
        (cell / 'etc').mkdir(parents=True)
        (cell / 'folder/html/segio/archive').mkdir(parents=True)
        (cell / 'etc/db.conf').write_text('SYNTHETIC_DB_PASSWORD_NEVER_COPY')
        return cell, cell / 'etc/segio-live-archive'

    def test_incomplete_upload_leaves_existing_installation_untouched(self):
        from export_schema_from_segio_config import SafeError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell, private = self.fixture(root)
            self.install(cell, private)
            before = {p.relative_to(private): p.read_bytes() for p in private.rglob('*') if p.is_file()}
            endpoint = cell / 'folder/html/segio/archive/live.php'
            original_endpoint = endpoint.read_bytes()
            tools = root / 'incomplete-tools'
            shutil.copytree(ROOT / 'tools', tools, ignore=shutil.ignore_patterns('__pycache__'))
            (tools / 'remote_ssh_export.py').unlink()
            with self.assertRaisesRegex(SafeError, 'remote_ssh_export.py'):
                self.install(cell, private, tools=tools)
            self.assertEqual(before, {p.relative_to(private): p.read_bytes() for p in private.rglob('*') if p.is_file()})
            self.assertEqual(endpoint.read_bytes(), original_endpoint)

    def test_refresh_preserves_local_settings_jobs_and_legacy_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell, private = self.fixture(root)
            template = str(root / 'custom-mail/{initial}/{user}/mbox')
            self.install(cell, private, template)
            config = private / 'config.json'
            settings = json.loads(config.read_text())
            settings['psql'] = '/opt/postgres/bin/psql'
            config.write_text(json.dumps(settings))
            job = private / 'jobs' / ('a' * 32)
            job.mkdir()
            (job / 'archive.zip').write_bytes(b'EXISTING_WEB_ARCHIVE')
            legacy = cell / 'folder/html/segio/archive/bridge.php'
            legacy.write_bytes(b'EXISTING_LEGACY_BRIDGE')
            ssh = root / ('.segio-ssh-' + 'c' * 32)
            ssh.mkdir(mode=0o700)
            (ssh / 'request.json').write_bytes(b'EXISTING_SSH_REQUEST')
            self.install(cell, private)
            self.assertEqual(json.loads(config.read_text()), settings)
            self.assertEqual((job / 'archive.zip').read_bytes(), b'EXISTING_WEB_ARCHIVE')
            self.assertEqual(legacy.read_bytes(), b'EXISTING_LEGACY_BRIDGE')
            self.assertEqual((ssh / 'request.json').read_bytes(), b'EXISTING_SSH_REQUEST')
            self.assertEqual((ssh.stat().st_mode & 0o777), 0o700)
            for name in set(shared.SSH_FILES + shared.WEB_CODE_FILES):
                self.assertEqual((private / 'code' / name).read_bytes(), (ROOT / 'tools' / name).read_bytes())
            self.install(cell, private, str(root / 'replacement/{initial}/{user}/mbox'))
            self.assertEqual(json.loads(config.read_text())['mail_template'], str(root / 'replacement/{initial}/{user}/mbox'))

    def test_installed_common_code_exports_both_protocols_without_cross_cleanup(self):
        from test_live_archive import create_fixture
        from segio_archive.archive import verify_archive
        from segio_archive.db_client import unpack_verified
        from segio_archive.mail_import import build_local_catalog
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cell, private = self.fixture(root)
            source = root / 'source'
            source.mkdir()
            settings, original = create_fixture(source)
            (cell / 'etc/db.conf').write_bytes((source / 'database.json').read_bytes())
            self.install(cell, private, settings['mail_template'])
            web_job = private / 'jobs' / ('b' * 32)
            web_job.mkdir()
            (web_job / 'request.json').write_text(json.dumps({
                'username': 'employee', 'approvals': True, 'mail': True, 'created_at': 1}))
            ssh_job = root / ('.segio-ssh-' + 'd' * 32)
            ssh_job.mkdir(mode=0o700)
            (ssh_job / 'request.json').write_text(json.dumps({
                'protocol': 'segio-ssh-archive/1', 'user': 'employee',
                'db_config': str(cell / 'etc/db.conf'), 'cell': str(cell),
                'mail_template': settings['mail_template'], 'psql': '/usr/bin/psql',
                'approvals': True, 'mail': True}))
            # New process: import the installed copies, not checkout modules.
            script = '''import json, sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(1, sys.argv[2])
import live_export_worker as web
import remote_ssh_export as ssh
from live_fixtures import synthetic_export
assert Path(web.__file__).parent == Path(sys.argv[1])
assert Path(ssh.__file__).parent == Path(sys.argv[1])
web.run_export = ssh.run_export = synthetic_export
web.run_job(sys.argv[3], 'b' * 32)
assert web.read_json(str(Path(sys.argv[4]) / 'status.json'))['state'] == 'ready'
ssh.prepare(sys.argv[5])
assert web.read_json(str(Path(sys.argv[5]) / 'status.json'))['state'] == 'ready'
ssh.verify_sources(sys.argv[5])
assert (Path(sys.argv[4]) / 'archive.zip').is_file()
subprocess.check_call([sys.executable, str(Path(sys.argv[1]) / 'remote_ssh_export.py'),
                       '--root', sys.argv[5], '--action', 'cleanup'])
assert (Path(sys.argv[4]) / 'archive.zip').is_file()
'''
            subprocess.run([sys.executable, '-c', script, str(private / 'code'), str(ROOT / 'tests'),
                            str(private / 'config.json'), str(web_job), str(ssh_job)],
                           check=True, capture_output=True, text=True, timeout=30)
            self.assertFalse(ssh_job.exists())
            result = root / 'download'
            result.mkdir()
            unpack_verified(web_job / 'archive.zip', result, 'employee')
            build_local_catalog(result, 'employee')
            self.assertEqual((result / 'mail/raw/000000.mbox').read_bytes(), original)
            self.assertTrue(verify_archive(result)['ok'])
            for file in result.rglob('*'):
                if file.is_file():
                    self.assertNotIn(b'OTHER_USER_MUST_NOT_EXPORT', file.read_bytes())


if __name__ == '__main__':
    unittest.main()
