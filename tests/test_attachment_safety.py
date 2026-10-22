from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from segio_archive.archive import verify_archive
from segio_archive import file_ops, portable_mail
from segio_archive.compact_mail import search
from segio_archive.mail_import import build_local_catalog
from segio_archive.portable_mail import prepare_archive, page_path, ISSUE_REPORT
import test_malformed_mail as malformed


def blocked_error(code=225):
    error=OSError(13,'SYNTHETIC_SECURITY_BLOCK')
    error.winerror=code
    return error


def message():
    mail=EmailMessage()
    mail['Subject']='Synthetic attachment test'
    mail.set_content('Safe body before and after the blocked attachment')
    mail.add_attachment(b'SYNTHETIC_ONLY_NOT_MALWARE',maintype='text',subtype='html',filename='blocked.html')
    mail.add_attachment(b'SAFE_ATTACHMENT',maintype='application',subtype='octet-stream',filename='safe.bin')
    return mail.as_bytes()


class AttachmentSafetyTests(unittest.TestCase):
    def fixture(self,root,payload=None):
        return malformed.MalformedMailTests().fixture(root,[malformed.NORMAL,payload or message(),malformed.NORMAL])

    def test_detached_html_descendant_and_huge_span_values_do_not_abort_conversion(self):
        # A malformed tree can leave a snapshotted child detached when its
        # rejected image parent is decomposed (the uploaded unwrap() site).
        soup=BeautifulSoup('<img src="remote"><p>Kept body</p>','html.parser')
        soup.img.append(soup.new_tag('unsupported'))
        with patch('segio_archive.portable_mail.BeautifulSoup',return_value=soup):
            result=portable_mail.sanitized_html('',{})
        self.assertIn('Kept body',result)
        self.assertNotIn('img',result)
        issues=set()
        text='<script>not executable</script><p>body</p>'
        with patch('segio_archive.portable_mail.sanitized_html',side_effect=ValueError('detached')):
            result=portable_mail.safe_html(text,{},issues)
        self.assertIn('html_as_text',issues)
        self.assertNotIn('<script>',result)
        result=portable_mail.sanitized_html('<table><tr><td colspan="'+('9'*5000)+'">cell</td></tr></table>',{})
        self.assertIn('cell',result)
        self.assertNotIn('colspan',result)

    def test_security_block_skips_only_that_attachment_and_persists_no_retry_record(self):
        for code in (225,226):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve()
                raw=self.fixture(root)
                replace=file_ops.replace_file
                attempts=[]
                def reject(source,target,*args):
                    if str(target).endswith('-blocked.html'):
                        attempts.append(str(target))
                        raise blocked_error(code)
                    return replace(source,target,*args)
                with patch.object(file_ops,'replace_file',side_effect=reject):
                    result=build_local_catalog(root,'employee')
                self.assertEqual(len(attempts),1)
                self.assertEqual(result['counts']['mail'],3)
                self.assertEqual(result['portable_mail']['unavailable_attachment_count'],1)
                self.assertEqual(result['portable_mail']['warning_count'],1)
                self.assertEqual(result['portable_mail']['raw_only_count'],0)
                self.assertTrue(verify_archive(root)['ok'])
                page=root/page_path('mail-000000001')
                soup=BeautifulSoup(page.read_text(),'html.parser')
                self.assertIn('Safe body',soup.select_one('main').get_text())
                self.assertEqual(len(soup.select('a.attachment')),1)
                self.assertIn('blocked.html',soup.select_one('.unavailable').get_text())
                self.assertEqual(next((page.parent/'attachments').glob('*-safe.bin')).read_bytes(),b'SAFE_ATTACHMENT')
                marker=(page.parent/'attachment-status.json').read_text()
                self.assertIn('attachment_security',marker)
                self.assertNotIn('SYNTHETIC_ONLY_NOT_MALWARE',marker)
                self.assertFalse((page.parent/'original.eml').exists())
                self.assertEqual((root/'mail/raw/000000.mbox').read_bytes(),raw)
                # Even a later retry of a partly completed conversion must not
                # recreate the security-blocked payload from the raw mailbox.
                manifest=json.loads((root/'manifest.json').read_bytes())
                manifest['local_processing']={'state':'pending'}
                (root/'manifest.json').write_text(json.dumps(manifest))
                with patch.object(file_ops,'replace_file',side_effect=reject):
                    prepare_archive(root)
                self.assertEqual(len(attempts),1)
                self.assertTrue(verify_archive(root)['ok'])

    def test_attachment_quarantined_after_completion_is_reported_without_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            raw=self.fixture(root)
            build_local_catalog(root,'employee')
            page=root/page_path('mail-000000001')
            attachment=next((page.parent/'attachments').glob('*-blocked.html'))
            attachment.unlink()
            write=portable_mail.write_generated
            def never_restore(root,name,*args):
                if name.endswith('-blocked.html'):
                    raise AssertionError('Quarantined payload was recreated')
                return write(root,name,*args)
            with patch.object(portable_mail,'write_generated',side_effect=never_restore):
                result=prepare_archive(root)
                prepare_archive(root)
            self.assertFalse(attachment.exists())
            self.assertTrue(verify_archive(root)['ok'])
            self.assertEqual(result['portable_mail']['unavailable_attachment_count'],1)
            self.assertIn('attachment_unavailable',(page.parent/'attachment-status.json').read_text())
            self.assertIn('없어졌거나',(root/ISSUE_REPORT).read_text())
            self.assertEqual((root/'mail/raw/000000.mbox').read_bytes(),raw)
            self.assertEqual(search(root,kind='mail')['total'],3)

    def test_disappearing_new_attachment_is_a_warning_not_a_false_success_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            replace=file_ops.replace_file
            def remove(source,target,*args):
                result=replace(source,target,*args)
                if str(target).endswith('-blocked.html'):Path(target).unlink()
                return result
            with patch.object(file_ops,'replace_file',side_effect=remove):
                result=build_local_catalog(root,'employee')
            self.assertEqual(result['portable_mail']['unavailable_attachment_count'],1)
            soup=BeautifulSoup((root/page_path('mail-000000001')).read_text(),'html.parser')
            self.assertEqual(len(soup.select('a.attachment')),1)
            self.assertTrue(verify_archive(root)['ok'])

    def test_old_failed_html_conversion_without_state_does_not_recreate_missing_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            page=root/page_path('mail-000000001')
            directory=page.parent/'attachments'
            directory.mkdir(parents=True)
            # This is the 0.6.1 failure shape: attachments already attempted,
            # one removed, HTML sanitizer failed before state.json was saved.
            (directory/'2-safe.bin').write_bytes(b'SAFE_ATTACHMENT')
            write=portable_mail.write_generated
            def no_rewrite(root,name,*args):
                if '/attachments/' in name:raise AssertionError('Old attachments must not be recreated')
                return write(root,name,*args)
            with patch.object(portable_mail,'write_generated',side_effect=no_rewrite):
                result=build_local_catalog(root,'employee')
            self.assertEqual(result['portable_mail']['unavailable_attachment_count'],1)
            self.assertIn('attachment_unverified',(page.parent/'attachment-status.json').read_text())
            self.assertEqual((directory/'2-safe.bin').read_bytes(),b'SAFE_ATTACHMENT')
            self.assertFalse((directory/'1-blocked.html').exists())
            self.assertTrue(verify_archive(root)['ok'])

    def test_blocked_attachment_plus_bad_mime_does_not_copy_payload_into_fallback_eml(self):
        body=(b'Subject: Security and invalid attachment\nContent-Type: multipart/mixed; boundary=parts\n\n'
              b'--parts\nContent-Type: text/html\nContent-Disposition: attachment; filename=blocked.html\n\nSYNTHETIC_ONLY\n'
              b'--parts\nContent-Type: application/octet-stream\nContent-Disposition: attachment; filename=bad.bin\nContent-Transfer-Encoding: base64\n\nA\n--parts--\n')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root,body)
            replace=file_ops.replace_file
            def reject(source,target,*args):
                if str(target).endswith('-blocked.html'):raise blocked_error()
                return replace(source,target,*args)
            with patch.object(file_ops,'replace_file',side_effect=reject):
                result=build_local_catalog(root,'employee')
            self.assertEqual(result['portable_mail']['source_only_count'],1)
            self.assertEqual(result['portable_mail']['raw_only_count'],0)
            self.assertEqual(result['portable_mail']['unavailable_attachment_count'],1)
            self.assertFalse(list(root.rglob('original.eml')))
            self.assertTrue(verify_archive(root)['ok'])

    def test_generic_access_denied_and_security_block_reading_raw_mail_remain_fatal(self):
        for raw_failure in (False,True):
            with self.subTest(raw=raw_failure),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve()
                self.fixture(root)
                if raw_failure:
                    def source(*args,**kwargs):
                        yield from ()
                        raise blocked_error()
                    manager=patch.object(portable_mail,'chunks',side_effect=source)
                else:
                    replace=file_ops.replace_file
                    def denied(source,target,*args):
                        if str(target).endswith('-blocked.html'):raise PermissionError(13,'ACCESS_DENIED')
                        return replace(source,target,*args)
                    manager=patch.object(file_ops,'replace_file',side_effect=denied)
                with manager,self.assertRaises(OSError):build_local_catalog(root,'employee')
                self.assertEqual(json.loads((root/'manifest.json').read_bytes())['local_processing']['state'],'pending')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            opened=Path.open
            class ClosingBlocked:
                def __init__(self,stream):self.stream=stream
                def close(self):
                    self.stream.close()
                    raise blocked_error(226)
                def __getattr__(self,key):return getattr(self.stream,key)
            def output(path,*args,**kwargs):
                stream=opened(path,*args,**kwargs)
                return ClosingBlocked(stream) if path.suffix=='.partial' else stream
            def unreadable():
                yield from ()
                raise blocked_error(225)
            with patch.object(Path,'open',output),self.assertRaises(OSError) as failure:
                portable_mail.write_generated(root,'mail/offline/mail-000000000/attachments/0-test.html',unreadable(),{},lambda:None)
            self.assertEqual(failure.exception.winerror,225)
