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
            for status,body,code in ((503,b'<html><input type="password" value="PRIVATE"></html>','BRIDGE_LOGIN_REQUIRED'),
                                     (503,b'<?php $secret="PRIVATE";?>','BRIDGE_PHP_SOURCE'),
                                     (404,b'<html><input type="password" value="PRIVATE"></html>','BRIDGE_LOGIN_REQUIRED'),
                                     (404,b'<?php $secret="PRIVATE";?>','BRIDGE_PHP_SOURCE'),
                                     (404,b'Fatal error: PRIVATE','BRIDGE_NOT_JSON')):
                with self.subTest(status=status,code=code):
                    client=self.client(Path(tmp))
                    client.context.request.get.return_value=response(status,'text/html',body)
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

    def test_transient_404_status_recovers_for_the_same_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            ready={'protocol':'segio-live-archive/1','username':'employee','job':client.job,'state':'ready'}
            replies=[response(404,'text/html',b'Not found PRIVATE'),
                     response(404,'text/html',b'<html>PRIVATE</html>'),
                     response(200,'application/json',json.dumps(ready).encode())]
            client.context.request.get.side_effect=replies
            with patch.object(client.config.cancel,'wait',return_value=False):
                self.assertEqual(client.poll_status(),ready)
            urls=[call.args[0] for call in client.context.request.get.call_args_list]
            self.assertEqual(len(urls),3)
            self.assertEqual(len(set(urls)),1)
            self.assertIn('action=status&job='+client.job,urls[0])
            client.context.request.post.assert_not_called()
            for reply in replies:reply.dispose.assert_called_once()

    def test_permanent_404_probes_login_without_starting_export_and_saves_safe_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            info={'protocol':'segio-live-archive/1','username':'employee',
                  'capabilities':{'live_db':True},'csrf':'PRIVATE_TOKEN'}
            client.context.request.get.side_effect=[response(404,'text/html',b'PRIVATE Not found') for _ in range(3)]+[
                response(200,'application/json',json.dumps(info).encode())]
            with patch.object(client.config.cancel,'wait',return_value=False):
                with self.assertRaisesRegex(BridgeResponseError,'로그인 연결은 정상.*BRIDGE_STATUS_NOT_FOUND') as caught:
                    client.poll_status()
            client.record_failure(caught.exception)
            text=(Path(tmp)/'last-client-failure.json').read_text()
            record=json.loads(text)
            self.assertEqual(record['phase'],'서버 추출 상태 확인')
            self.assertEqual(record['response'],{'action':'status','http_status':404,'content_type':'text/html','body_kind':'other'})
            self.assertEqual(record['status_checks'],{'attempts':3,'bridge_state':'available'})
            self.assertEqual(client.context.request.get.call_count,4)
            self.assertTrue(client.context.request.get.call_args.args[0].endswith('?action=info'))
            self.assertEqual(client.context.request.get.call_args.kwargs['timeout'],10000)
            self.assertIsNone(client.csrf)
            client.context.request.post.assert_not_called()
            for secret in ('PRIVATE','employee','example.invalid',client.job):self.assertNotIn(secret,text)

    def test_permanent_404_distinguishes_missing_endpoint_authentication_and_configuration(self):
        config_body=json.dumps({'protocol':'segio-live-archive/1','error':'service_not_configured',
                               'checks':{'config_exists':False,'password':'PRIVATE'}}).encode()
        cases=[(response(404,'text/html',b'PRIVATE missing'),'not_found'),
               (response(401,'application/json',b'PRIVATE native_login_required'),'login_required'),
               (response(503,'application/json',config_body),'service_not_configured'),
               (response(503,'text/html',b'PRIVATE unavailable'),'unavailable'),
               (response(200,'application/json',json.dumps({'protocol':'segio-live-archive/1',
                    'username':'PRIVATE_OTHER','capabilities':{'live_db':True}}).encode()),'unavailable')]
        with tempfile.TemporaryDirectory() as tmp:
            for probe,state in cases:
                with self.subTest(state=state):
                    client=self.client(Path(tmp))
                    client.context.request.get.side_effect=[response(404,'text/html',b'PRIVATE') for _ in range(3)]+[probe]
                    with patch.object(client.config.cancel,'wait',return_value=False):
                        with self.assertRaises(BridgeResponseError) as caught:client.poll_status()
                    client.record_failure(caught.exception)
                    record=json.loads((Path(tmp)/'last-client-failure.json').read_text())
                    self.assertEqual(record['status_checks']['bridge_state'],state)
                    self.assertEqual(record['response']['action'],'status')
                    if state=='service_not_configured':self.assertEqual(record['server_checks'],{'config_exists':False})
                    if 'bridge_response' in record['status_checks']:
                        self.assertEqual(record['status_checks']['bridge_response']['action'],'info')
                    self.assertNotIn('PRIVATE',json.dumps(record))
                    self.assertEqual(client.context.request.get.call_count,4)
                    client.context.request.post.assert_not_called()

    def test_explicit_missing_job_json_is_not_retried_or_replaced_with_a_new_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            client.context.request.get.side_effect=[
                response(404,'application/json',b'{"protocol":"segio-live-archive/1","error":"job_not_found"}'),
                response(200,'application/json',b'{"protocol":"segio-live-archive/1","username":"employee","capabilities":{"live_db":true}}')]
            with self.assertRaisesRegex(BridgeResponseError,'job_not_found') as caught:client.poll_status()
            client.record_failure(caught.exception)
            record=json.loads((Path(tmp)/'last-client-failure.json').read_text())
            self.assertEqual(record['error'],'job_not_found')
            self.assertEqual(record['status_checks'],{'attempts':1,'bridge_state':'available'})
            self.assertEqual(client.context.request.get.call_count,2)
            client.context.request.post.assert_not_called()

    def test_cancel_during_404_probe_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            with patch.object(client,'live_call',side_effect=Cancelled()):
                with self.assertRaises(Cancelled):
                    client.status_not_found(BridgeResponseError('missing','BRIDGE_NOT_JSON',
                        {'action':'status','http_status':404,'content_type':'text/html','body_kind':'other'}),3)
            self.assertEqual(client.phase,'서버 추출 상태 확인')

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

    def test_service_config_error_retains_only_safe_checks_and_exact_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=self.client(Path(tmp))
            checks={'config_exists':False,'config_readable':False,'config_json_object':False,
                    'php_path_restriction':True,'php_safe_mode':False,
                    'password':'PRIVATE_PASSWORD','config_path':'PRIVATE_PATH'}
            body={'protocol':'segio-live-archive/1','error':'service_not_configured','checks':checks,
                  'secret':'PRIVATE_RESPONSE'}
            client.context.request.get.return_value=response(503,'application/json',json.dumps(body).encode())
            with self.assertRaisesRegex(BridgeResponseError,'설치 경로.*service_not_configured') as caught:
                client.live_call('info')
            client.record_failure(caught.exception)
            text=(Path(tmp)/'last-client-failure.json').read_text()
            record=json.loads(text)
            self.assertEqual(record['error'],'service_not_configured')
            self.assertEqual(record['response']['http_status'],503)
            self.assertEqual(record['server_checks'],{key:value for key,value in checks.items() if type(value) is bool})
            self.assertNotIn('PRIVATE',text)
            self.assertNotIn('password',text)


if __name__=='__main__':unittest.main()
