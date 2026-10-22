import json
import base64
import os
import subprocess
from html import escape
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import export_approval_db as exporter


class DBExportTests(unittest.TestCase):
    def test_cancelled_database_communication_terminates_only_its_own_child(self):
        import time
        process=subprocess.Popen([sys.executable,'-c','import sys,time; sys.stdin.buffer.read(); time.sleep(60)'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        started=time.monotonic()
        def checkpoint():
            if time.monotonic()-started>.2:raise exporter.SafeError('cancelled')
        try:
            with self.assertRaisesRegex(exporter.SafeError,'cancelled'):
                exporter.communicate_cancellable(process,b'SELECT 1;',checkpoint)
            self.assertIsNotNone(process.poll())
            self.assertLess(time.monotonic()-started,5)
        finally:
            if process.poll() is None:process.kill()
            process.wait()
    @unittest.skipIf(os.name == 'nt', 'Legacy POSIX filename encodings')
    def test_korean_native_paths_under_ascii_locale_and_ambiguous_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            upload=root/'cell/data/approval/upload'
            upload.mkdir(parents=True)
            name='한글+첨부.txt'
            native=os.fsencode(upload)+b'/'+name.encode('cp949')
            with open(native,'wb') as stream:stream.write(b'SYNTHETIC_EXACT_BYTES')
            bundle=exporter.Bundle(str(root/'archive'),str(root/'cell'),'employee')
            with patch.object(exporter.sys,'getfilesystemencoding',return_value='ascii'):
                result=bundle.attachment({'no':'1','fname':name,'fpath':name},'1')
                self.assertEqual((root/'archive'/result['path']).read_bytes(),b'SYNTHETIC_EXACT_BYTES')
                self.assertEqual(bundle.local_file(name.encode('cp949'),upload=True),native)
                from urllib.parse import quote_from_bytes
                encoded='data/approval/upload/'+quote_from_bytes(name.encode('cp949'))
                self.assertEqual(bundle.local_file(encoded,upload=True),native)
                (upload/name).write_bytes(b'OTHER_FILE_SAME_DISPLAYED_NAME')
                self.assertIsNone(bundle.local_file(name,upload=True))
                self.assertIsNone(bundle.local_file('../../../../private.txt',upload=True))

    @unittest.skipIf(os.name == 'nt', 'Legacy POSIX filename encodings')
    def test_real_export_roundtrip_with_legacy_runtime_when_available(self):
        from segio_archive.db_client import unpack_verified
        from segio_archive.archive import verify_archive
        runtimes=[sys.executable]
        if os.environ.get('SEGIO_TEST_PYTHON26'):
            runtimes.append(os.environ['SEGIO_TEST_PYTHON26'])
        for executable in runtimes:
            with self.subTest(runtime=executable), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                environment=dict(os.environ,LC_ALL='C',PYTHONCOERCECLOCALE='0',PYTHONUTF8='0')
                result=subprocess.run([executable,str(Path(__file__).parent/'legacy_export_fixture.py'),str(root)],
                                      env=environment,capture_output=True,check=True)
                summary=json.loads(result.stdout)
                self.assertTrue(summary['ok'])
                self.assertEqual(summary['attachments'],2)
                target=root/'verified'
                target.mkdir()
                unpack_verified(root/'output/segio-employee.zip',target,'employee')
                self.assertTrue(verify_archive(target)['ok'])
                for number in range(2):
                    self.assertEqual((target/('documents/1/attachments/%d.txt'%(number+1))).read_bytes(),
                                     b'SYNTHETIC_ATTACHMENT_KEPT_EXACTLY_'+str(number).encode('ascii'))

    def test_encoding_diagnostic_contains_no_document_fragments(self):
        import live_export_worker as worker
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'private.csv'
            source.write_text('context\n'+(b'PRIVATE_DB_TEXT_'+bytes([255])).hex()+'\n')
            try:
                exporter.read_rows(str(source))
            except UnicodeError as exc:
                code,diagnostic=worker.failure_details(exc,'database')
            else:
                self.fail('Invalid UTF-8 must not be silently rewritten')
            self.assertEqual(code,'db_encoding_failed')
            self.assertEqual(diagnostic['exception_type'],'UnicodeDecodeError')
            self.assertEqual(diagnostic['encoding'],'utf-8')
            self.assertEqual(diagnostic['site']['function'],'read_rows')
            self.assertIsInstance(diagnostic['site']['line'],int)
            self.assertNotIn('PRIVATE',json.dumps(diagnostic))
            self.assertNotIn(str(source),json.dumps(diagnostic))

    def test_sqlstate_errors_are_specific_and_credentials_never_escape(self):
        cases = ((b'28P01', 'db_authentication_failed'), (b'42501', 'db_permission_denied'),
                 (b'42P01', 'db_schema_mismatch'), (b'42703', 'db_schema_mismatch'),
                 (b'57014', 'db_query_timeout'), (b'53100', 'server_disk_full'))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root/'db.conf'
            secret = 'PRIVATE_PASSWORD_AND_SQL_VALUE'
            config.write_text('DBMS=pgsql\nDBNAME=cell_000\nUSER=fixture\nPW='+secret+'\n')
            for state, expected in cases:
                process = Mock(returncode=1)
                process.communicate.return_value = (b'', b'ERROR:  '+state+b': '+secret.encode())
                def launch(*args, **kwargs):
                    passfile = Path(kwargs['env']['PGPASSFILE'])
                    self.assertTrue(passfile.is_file())
                    if sys.platform != 'win32':
                        self.assertEqual(passfile.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn(secret, repr(args))
                    return process
                with self.subTest(state=state), patch.object(exporter.subprocess, 'Popen', side_effect=launch):
                    with self.assertRaises(exporter.SafeError) as caught:
                        exporter.psql_readonly(str(config), str(root), ['SELECT 1;'])
                    self.assertEqual(caught.exception.code, expected)
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertFalse((root/'pgpass').exists())

    def test_authenticated_employee_without_approval_profile_can_archive_empty_approvals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell = root/'cell'
            (cell/'data').mkdir(parents=True)
            model = json.loads((Path(exporter.HERE)/'approval_db_columns.json').read_text())
            rows = {table: [] for table in model}
            counts = {table: 0 for table in model}
            with patch.object(exporter, 'query_database', return_value=(rows, counts)):
                # Administrator ID spelling checks remain strict in CLI mode.
                with self.assertRaisesRegex(exporter.SafeError, 'Target user'):
                    exporter.run_export('unused', 'employee', str(root), cell=str(cell))
                folder, archive = exporter.run_export('unused', 'employee', str(root), cell=str(cell), live_scope=True)
            manifest = json.loads((Path(folder)/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['counts']['approval'], 0)
            self.assertTrue(any('0개' in warning for warning in manifest['warnings']))
            self.assertTrue(Path(archive).is_file())

    def test_encoded_body_is_decoded_then_sanitized_without_changing_literal_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = exporter.Bundle(tmp, tmp, "employee")
            original_html = '<p>한글 A+B 100% &lt;예시&gt;</p><script>evil()</script><img src="https://external.invalid/x" onerror="evil()">'
            stored = quote_plus(escape(original_html))
            self.assertTrue(stored.startswith("%26lt%3Bp%26gt"))
            rendered, plain = bundle.freeze(stored)
            self.assertIn("한글 A+B 100% &lt;예시&gt;", rendered)
            self.assertIn("한글 A+B 100% <예시>", plain)
            for unsafe in ("<script", "onerror", "evil()", "https:"):
                self.assertNotIn(unsafe, rendered)
            ordinary, ordinary_text = bundle.freeze('<p>literal &lt;em&gt; A+B /?x=%26lt</p>')
            self.assertIn("&lt;em&gt; A+B /?x=%26lt", ordinary)
            self.assertEqual(bundle.display_decoding["urlencoded_html"], 1)
            self.assertEqual(bundle.display_decoding["escaped_html"], 1)
            literal, literal_text = bundle.freeze("&lt;script&gt;code example&lt;/script&gt;")
            self.assertIn("&lt;script&gt;code example&lt;/script&gt;", literal)

    def test_raw_jpeg_signature_becomes_an_image_and_arbitrary_base64_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = exporter.Bundle(tmp, tmp, "employee")
            jpeg = (Path(__file__).parent / "fixtures/signature.jpg").read_bytes()
            stored = base64.b64encode(jpeg).decode()
            self.assertTrue(stored.startswith("/9j/"))
            rendered, plain = bundle.freeze(stored, signature=True)
            self.assertIn('src="data:image/jpeg;base64,', rendered)
            self.assertEqual(plain, "")
            self.assertEqual(bundle.display_decoding["base64_signature"], 1)
            bad = base64.b64encode(b"<svg onload='evil()'></svg>").decode()
            self.assertIsNone(exporter.signature_data_uri(bad))
            self.assertIsNone(exporter.signature_data_uri("/9j/invalid!"))

    def test_attachment_traversal_symlinks_and_changed_files_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell = root / "cell"
            upload = cell / "data/approval/upload"
            upload.mkdir(parents=True)
            (upload / "ok.txt").write_text("selected attachment")
            outside = root / "private.txt"
            outside.write_text("SYNTHETIC_SECRET_MUST_NOT_COPY")
            references = [str(outside), "../../../../private.txt"]
            if sys.platform != "win32":
                (upload / "symlink.txt").symlink_to(outside)
                references.append("symlink.txt")
            folder = root / "archive"
            folder.mkdir()
            bundle = exporter.Bundle(str(folder), str(cell), "employee")
            for reference in references:
                self.assertIsNone(bundle.attachment({"no": "1", "fname": "original.txt", "fpath": reference}, "10"))
            with patch.object(bundle, "same_file", return_value=False):
                self.assertIsNone(bundle.attachment({"no": "2", "fname": "original.txt", "fpath": "ok.txt"}, "10"))
            self.assertFalse(list(folder.rglob("*.txt")))
            self.assertEqual(bundle.files, {})
            self.assertTrue(any(r["kind"] == "attachment_copy_failed_or_changed" for r in bundle.issues))

    def test_dynamic_inline_image_maps_only_the_verified_local_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "cell/data/approval/app_images"
            images.mkdir(parents=True)
            (images / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nSYNTHETIC_IMAGE")
            bundle = exporter.Bundle(str(root / "archive"), str(root / "cell"), "employee")
            rendered, plain = bundle.freeze('''<img src="/segio/works/approval/img_insert.php?mode=app_images&amp;fname=pic.png" onerror="evil()"><script>evil()</script><style>@import url(https://external.invalid)</style><iframe src="file:///etc/passwd"></iframe><a href="javascript:evil()">text</a><p style="background:url(https://external.invalid);color:red">body</p>''')
            self.assertIn("data:image/png;base64,", rendered)
            for value in ("onerror", "evil()", "javascript:", "file:", "https:", "<script", "<iframe", "<style"):
                self.assertNotIn(value, rendered)
            self.assertIn("color:red", rendered)
            self.assertIsNone(bundle.image("/segio/works/approval/img_insert.php?mode=app_images&fname=../../etc/db.conf"))

    @unittest.skipIf(sys.platform == "win32", "Uses a POSIX fake psql executable")
    def test_client_failure_and_incomplete_success_do_not_leave_credentials_or_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell = root / "cell"
            (cell / "data").mkdir(parents=True)
            (cell / "etc").mkdir()
            config = cell / "etc/db.conf"
            secret = "SYNTHETIC_PASSWORD_MUST_NOT_PRINT"
            config.write_text("DBMS=postgresql\nDBNAME=cell_000\nUSER=fixture\nPW=" + secret + "\n")
            original = config.read_bytes()
            client = root / "psql"
            for exit_code in (2, 0):
                client.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '" + secret + "' >&2\nexit " + str(exit_code) + "\n")
                client.chmod(0o700)
                with self.assertRaises(exporter.SafeError) as caught:
                    exporter.run_export(str(config), "employee", str(root), binary=str(client))
                self.assertNotIn(secret, str(caught.exception))
                self.assertFalse(list(root.glob("segio-db-*")))
                self.assertEqual(config.read_bytes(), original)

    def test_csv_transport_preserves_unicode_null_empty_and_literal_null_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "transport.csv"
            values = [None, "", "__SEGIO_NULL__", "문서\n둘째 줄", "a,b\\c"]
            p.write_text("value\n" + "\n".join(exporter.NULL if v is None else v.encode().hex() if v else '""' for v in values) + "\n")
            self.assertEqual([r["value"] for r in exporter.read_rows(str(p))], values)


if __name__ == "__main__":
    unittest.main()
