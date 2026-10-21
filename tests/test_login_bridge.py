"""Run the real PHP bridge behind a synthetic native-authentication router.

Set SEGIO_TEST_PHP to a PHP CLI executable to include the integration tests.
The router is only a test fixture; NEVER deploy it as a SEGIO login handler.
"""
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import urllib.request

from segio_archive.archive import verify_archive
from segio_archive.db_client import PreparedDBCollector as DBCollector, unpack_verified
from segio_archive.web import CollectionError, Config, canonical
from test_db_client import make_archive

ROOT = Path(__file__).resolve().parents[1]
PHP = os.environ.get('SEGIO_TEST_PHP') or shutil.which('php')
PASSWORD = 'synthetic-password-never-save'
ROUTER = r'''<?php
/* Synthetic native session, not part of the deployed bridge. */
session_name('SEGIO_TEST_SESSION');
/* CLI -d INI parsing truncates unquoted paths at ~, including RUNNER~1. */
session_save_path(__DIR__.'/sessions');
if (!@session_start()) {
    header('HTTP/1.1 500 Internal Server Error', true, 500);
    echo 'Test fixture session storage failed'; exit;
}
$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
if ($path==='/__bridge_test_ready__') {
    header('Content-Type: application/json');
    echo json_encode(array('fixture'=>'__FIXTURE_ID__')); return;
}
if ($path==='/login' && $_SERVER['REQUEST_METHOD']==='POST') {
    if (isset($_POST['uid'], $_POST['password']) &&
        in_array($_POST['uid'], array('employee','other'), true) &&
        $_POST['password']==='synthetic-password-never-save') {
        $_SESSION['uid']=$_POST['uid']; header('Location: /home'); exit;
    }
    header('HTTP/1.1 401 Unauthorized'); echo 'Login refused'; exit;
}
if ($path==='/segio/archive/bridge.php') {
    if (isset($_SESSION['uid'])) {
        $_ENV['REMOTE_USER']=$_SESSION['uid']; $_ENV['REMOTE_USERLEVEL']='1';
    }
    session_write_close(); include __DIR__.'/bridge.php'; return;
}
if ($path==='/home' && isset($_SESSION['uid'])) {
    echo '<html><body>Authenticated synthetic home</body></html>'; return;
}
echo '<html><body><form method="post" action="/login"><input name="uid"><input name="password" type="password"><button>Login</button></form></body></html>';
'''


def local_opener(jar=None):
    # This is a real loopback integration test, independent of the global
    # urlopen opener, OS proxy configuration and unrelated network mocks.
    handlers = [urllib.request.ProxyHandler({})]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


class NativePHPFixture:
    @classmethod
    def setUpClass(cls):
        # Exercise the INI-special ~ and spaces on every OS, not only Windows
        # runners whose temporary path contains the short user name RUNNER~1.
        cls.temp = tempfile.TemporaryDirectory(prefix='segio login-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)/'RUNNER~1'/'bridge fixture'
        cls.root.mkdir(parents=True)
        private = cls.root/'private'
        (private/'users').mkdir(parents=True)
        version = 'a'*32
        snapshot = private/'snapshots'/version
        snapshot.mkdir(parents=True)
        fixture = make_archive(cls.root)
        body = fixture.read_bytes()
        fixture.rename(snapshot/'archive.zip')
        cls.record = {'username':'employee', 'version':version, 'bytes':len(body),
                      'sha256':hashlib.sha256(body).hexdigest(), 'counts':{'approval':1},
                      'prepared_at':'2026-10-20T00:00:00Z'}
        name = hashlib.sha256(b'employee').hexdigest()+'.json'
        (private/'users'/name).write_text(json.dumps(cls.record), encoding='utf-8')
        php = (ROOT/'tools/login_archive_bridge.php').read_text(encoding='utf-8')
        # PHP single-quoted literals must preserve Windows paths and any quote
        # in the temporary parent path. Avoid relying on the host encoding.
        private_literal = private.as_posix().replace("'", "\\'")
        (cls.root/'bridge.php').write_text(php.replace('__PRIVATE_ROOT__', private_literal), encoding='utf-8')
        cls.fixture_id = os.urandom(16).hex()
        (cls.root/'router.php').write_text(ROUTER.replace('__FIXTURE_ID__', cls.fixture_id), encoding='utf-8')
        (cls.root/'sessions').mkdir()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cls.url = f'http://127.0.0.1:{port}'
        cls.process = subprocess.Popen([PHP, '-n', '-S', f'127.0.0.1:{port}', '-t', cls.root.as_posix(),
                                        (cls.root/'router.php').as_posix()],
                                       cwd=cls.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop_server)
        opener = local_opener()
        for attempt in range(100):
            try:
                with opener.open(cls.url+'/__bridge_test_ready__', timeout=1) as response:
                    if response.status==200 and json.load(response)=={'fixture':cls.fixture_id}:
                        break
                    raise RuntimeError('Unexpected response from test PHP router')
            except urllib.error.HTTPError as exc:
                with exc:
                    detail = exc.read(256)
                raise RuntimeError(f'Test PHP readiness HTTP {exc.code}: {detail!r}') from None
            except (OSError, urllib.error.URLError):
                if cls.process.poll() is not None:
                    raise RuntimeError('Test PHP process failed to start')
                time.sleep(.05)
        else:
            raise RuntimeError('Test PHP process did not start')

    @classmethod
    def stop_server(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
        cls.process.wait(timeout=10)

    def assert_bridge_error(self, opener, request, status, code, **kwargs):
        # Keep the HTTPError contract: accepting a 200 JSON error would hide a
        # broken server status. Also identify which endpoint/error was tested.
        with self.assertRaises(urllib.error.HTTPError,
                               msg=f'Expected HTTP {status} / {code}') as caught:
            with opener.open(request, timeout=5, **kwargs) as response:
                self.fail(f'Expected HTTP {status} / {code}, received HTTP {response.status}: '
                          f'{response.read(256)!r}')
        with caught.exception as response:
            self.assertEqual(response.code, status)
            payload = json.load(response)
        self.assertEqual(payload.get('protocol'), 'segio-login-archive/1')
        self.assertEqual(payload.get('error'), code)

    def logged_in(self, username='employee'):
        jar = http.cookiejar.CookieJar()
        opener = local_opener(jar)
        data = urllib.parse.urlencode({'uid':username, 'password':PASSWORD}).encode()
        with opener.open(self.url+'/login', data=data, timeout=5) as response:
            self.assertEqual(response.status, 200)
        return opener


@unittest.skipUnless(PHP, 'Optional PHP CLI needed: set SEGIO_TEST_PHP')
class LoginBridgeTests(NativePHPFixture, unittest.TestCase):
    def test_no_native_identity_rejects_spoofed_user_headers_and_query_ids(self):
        request = urllib.request.Request(self.url+'/segio/archive/bridge.php?action=info&uid=employee',
                                         headers={'Remote-User':'employee', 'X-Remote-User':'employee',
                                                  'Authorization':'Basic ZW1wbG95ZWU6cHc='})
        self.assert_bridge_error(local_opener(), request, 401, 'native_login_required')

    def test_global_urlopen_mock_and_system_proxy_do_not_replace_loopback_responses(self):
        # A broad mock returning HTTP 200 used to turn assertRaises(HTTPError)
        # into "HTTPError not raised". This test must still reach the real PHP
        # bridge with its normal HTTP error handlers and a fresh cookie jar.
        with patch('urllib.request.urlopen', return_value=Mock(status=200)) as mocked_open, \
             patch('urllib.request.getproxies', return_value={'http':'http://127.0.0.1:1'}):
            self.assert_bridge_error(local_opener(), self.url+'/segio/archive/bridge.php',
                                     401, 'native_login_required')
            mocked_open.assert_not_called()

    def test_other_employee_cannot_get_published_employee_archive(self):
        opener = self.logged_in('other')
        with opener.open(self.url+'/segio/archive/bridge.php?action=info') as response:
            result = json.load(response)
        self.assertEqual(result['username'], 'other')
        self.assertIsNone(result['archive'])
        self.assert_bridge_error(opener, self.url+'/segio/archive/bridge.php?action=download&version='+self.record['version'],
                                 404, 'archive_not_prepared')
        self.assert_bridge_error(opener, self.url+'/segio/archive/bridge.php?action=download&uid=employee',
                                 400, 'unexpected_parameter')

    def test_get_only_and_snapshot_version_checked_before_download(self):
        opener = self.logged_in()
        endpoint = self.url+'/segio/archive/bridge.php'
        self.assert_bridge_error(opener, endpoint+'?action=download&version='+'b'*32,
                                 409, 'archive_updated')
        self.assert_bridge_error(opener, endpoint, 405, 'read_only', data=b'action=delete')
        with opener.open(endpoint+'?action=download&version='+self.record['version']) as response:
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(), self.record['sha256'])

    def test_browser_employee_login_download_verify_and_wrong_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            config = Config(self.url+'/', 'employee', output, mail=False, pdf=False,
                            allow_http=True, headless=True, timeout_ms=5000,
                            browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
            logs = []
            result = DBCollector(config, logs.append).run(PASSWORD)
            self.assertEqual(result.name, 'segio-employee')
            self.assertTrue(verify_archive(result)['ok'])
            self.assertTrue((output/'segio-employee.zip').is_file())
            for file in output.rglob('*'):
                if file.is_file():
                    self.assertNotIn(PASSWORD.encode(), file.read_bytes())
                    self.assertNotIn(b'SEGIO_TEST_SESSION', file.read_bytes())
            self.assertNotIn(PASSWORD, '\n'.join(logs))
            other_output = output/'failed'
            config.output = other_output
            with self.assertRaises(CollectionError):
                DBCollector(config).run('wrong-password')
            self.assertEqual({p.name for p in other_output.iterdir()},{'last-client-failure.json'})
            # A manual login with another account must not produce an archive
            # labelled as the requested employee.
            client = DBCollector(config)
            def different_login(collector, password, **kwargs):
                collector.navigate(self.url+'/')
                collector.page.locator('[name=uid]').fill('other')
                collector.page.locator('[name=password]').fill(PASSWORD)
                collector.login_actions.add(canonical(self.url+'/login'))
                collector.page.locator('button').click()
            with patch('segio_archive.db_client.PreparedDBCollector.login', different_login):
                with self.assertRaisesRegex(CollectionError, '실제 로그인 계정'):
                    client.run('unused')
            self.assertEqual({p.name for p in other_output.iterdir()},{'last-client-failure.json'})


@unittest.skipIf(sys.platform=='win32', 'POSIX server publisher')
class PublicationTests(unittest.TestCase):
    def test_preparation_exposes_only_snapshot_and_preserves_db_config(self):
        sys.path.insert(0, str(ROOT/'tools'))
        import prepare_login_archive as publisher
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell = root/'cell'
            (cell/'folder/html/segio').mkdir(parents=True)
            (cell/'etc').mkdir()
            config = cell/'etc/db.conf'
            secret = b'SYNTHETIC_DB_PASSWORD_NEVER_PUBLISH'
            config.write_bytes(secret)
            private = root/'private'
            def export(config, username, output, **kwargs):
                base = Path(output)
                archive = make_archive(base, username)
                folder = base/'archive'
                folder.mkdir()
                unpack_verified(archive, folder, username)
                return str(folder), str(archive)
            account = types.SimpleNamespace(pw_uid=1234, pw_gid=os.getgid())
            with patch.object(publisher.os, 'geteuid', return_value=0), \
                 patch.object(publisher.os, 'chown'), \
                 patch.object(publisher.pwd, 'getpwnam', return_value=account), \
                 patch.object(publisher, 'run_export', side_effect=export):
                record = publisher.prepare('employee', 'synthetic-web', str(config), str(private))
            self.assertEqual(config.read_bytes(), secret)
            self.assertEqual((private.stat().st_mode & 0o777), 0o750)
            zipped = private/'snapshots'/record['version']/'archive.zip'
            self.assertEqual((zipped.stat().st_mode & 0o777), 0o640)
            self.assertEqual(hashlib.sha256(zipped.read_bytes()).hexdigest(), record['sha256'])
            endpoint = cell/'folder/html/segio/archive/bridge.php'
            self.assertEqual(endpoint.parent.stat().st_mode & 0o777, 0o755)
            self.assertNotIn('__PRIVATE_ROOT__', endpoint.read_text(encoding='utf-8'))
            for path in private.rglob('*'):
                if path.is_file():
                    self.assertNotIn(secret, path.read_bytes())
            self.assertEqual([p.name for p in endpoint.parent.iterdir()], ['bridge.php'])


if __name__ == '__main__':
    unittest.main()
