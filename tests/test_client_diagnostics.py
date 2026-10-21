import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from segio_archive.client_diagnostics import BridgeResponseError, write_failure
from segio_archive.db_client import DBCollector
from segio_archive.web import Cancelled, Config


def response(status,media,body):
    value=Mock(status=status,headers={'content-type':media,'location':'https://PRIVATE.invalid/SECRET'})
    value.body.return_value=body
    return value


class ClientDiagnosticTests(unittest.TestCase):
    def client(self,output):
        client=DBCollector(Config('https://example.invalid/','employee',output))
        client.context=Mock()
        client.job='a'*32
        return client

    def test_nonjson_error_retains_safe_response_metadata_and_no_private_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            reply=response(200,'text/html; PRIVATE_HEADER=SECRET',b'<html>PRIVATE_HTML_COOKIE_EMPLOYEE</html>')
            client.context.request.get.return_value=reply
            with self.assertRaisesRegex(BridgeResponseError,'HTTP 200.*BRIDGE_NOT_JSON') as caught:
                client.live_call('status')
            client.record_failure(caught.exception)
            record=Path(tmp)/'last-client-failure.json'
            text=record.read_text()
            value=json.loads(text)
            self.assertEqual(value['error'],'BRIDGE_NOT_JSON')
            self.assertEqual(value['response'],{'action':'status','http_status':200,'content_type':'text/html','body_kind':'html'})
            self.assertEqual(value['phase'],'서버 추출 상태 확인')
            for private in ('PRIVATE','SECRET','employee','example.invalid',str(Path(tmp))):
                self.assertNotIn(private,text)
            if os.name!='nt':self.assertEqual(record.stat().st_mode&0o777,0o600)
            reply.dispose.assert_called_once()

    def test_login_and_php_source_responses_are_rejected_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            for body,code in ((b'<html><input type="password" value="PRIVATE"></html>','BRIDGE_LOGIN_REQUIRED'),
                              (b'<?php $secret="PRIVATE";?>','BRIDGE_PHP_SOURCE')):
                with self.subTest(code=code):
                    client=self.client(Path(tmp))
                    client.context.request.get.return_value=response(503,'text/html',body)
                    with self.assertRaisesRegex(BridgeResponseError,code) as caught:
                        client.poll_status()
                    self.assertEqual(client.context.request.get.call_count,1)
                    self.assertNotIn('PRIVATE',str(caught.exception))
            client=self.client(Path(tmp))
            client.context.request.get.return_value=response(200,'text/html',b'<input type="password">')
            self.assertIsNone(client.live_call('info',pending_login=True))
            self.assertFalse((Path(tmp)/'last-client-failure.json').exists())

    def test_transient_html_status_responses_retry_get_but_never_replay_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            ready={'protocol':'segio-live-archive/1','username':'employee','job':'a'*32,'state':'ready'}
            client.context.request.get.side_effect=[response(503,'text/html',b'<html>busy</html>'),
                response(502,'text/html',b'<html>gateway</html>'),response(200,'Application/JSON; charset=UTF-8',json.dumps(ready).encode())]
            with patch.object(client.config.cancel,'wait',return_value=False):
                self.assertEqual(client.poll_status(),ready)
            self.assertEqual(client.context.request.get.call_count,3)
            client.context.request.post.assert_not_called()
            client.context.request.post.return_value=response(503,'text/html',b'<html>busy</html>')
            with self.assertRaises(BridgeResponseError):client.live_call('start',post=True)
            self.assertEqual(client.context.request.post.call_count,1)

    def test_user_cancel_interrupts_status_retry_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            client.context.request.get.return_value=response(503,'text/html',b'<html>busy</html>')
            def cancelled(seconds):
                client.config.cancel.set()
                return True
            with patch.object(client.config.cancel,'wait',side_effect=cancelled):
                with self.assertRaises(Cancelled):client.poll_status()
            self.assertEqual(client.context.request.get.call_count,1)

    def test_generic_failure_log_does_not_save_exception_text_or_arbitrary_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=write_failure(Path(tmp),RuntimeError('https://PRIVATE.invalid/?pass=SECRET [SECRET_CODE]'),'PRIVATE_PHASE')
            value=json.loads(path.read_text())
            self.assertEqual(value['error'],'COLLECTION_ERROR')
            self.assertEqual(value['phase'],'unknown')
            self.assertNotIn('PRIVATE',path.read_text())
            self.assertNotIn('SECRET',path.read_text())


if __name__=='__main__':unittest.main()
