"""Portable product regressions; no credentials, network inference or research corpus."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from carerelay.app import create_server, initialize_budget
from carerelay.discharge_flow import FlowError, Document, validate_explanation
from carerelay.discharge_http import WorkspaceServer
from carerelay.discharge_service import WorkspaceStore
from carerelay.documents import DocumentError
from carerelay.gemma_ocr import GemmaOCR, MODEL
from carerelay.local_pdf import extract_local_pdf
from carerelay.api_protocol import TASK_ID, trace_task, _TimedHTTPSConnection

ASSETS = Path(__file__).resolve().parents[1] / 'src/carerelay/assets/demo'


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = WorkspaceStore(self.path/'cases.db', local_ocr=False)
        self.token = self.store.create('before_delivery')

    def tearDown(self):
        self.temp.cleanup()

    def act(self, role, action, **payload):
        state = self.store.view(self.token, role)
        return self.store.mutate(self.token, role, state['csrf'], action, {'revision': state['revision'], **payload})

    def test_delivery_thread_followup_and_patient_resolution(self):
        self.assertEqual(self.store.view(self.token, 'patient')['documents'], [])
        self.act('staff', 'confirm_bundle', documents=['letter', 'rehab'], reviewed=True)
        self.act('staff', 'deliver_documents')
        self.assertEqual(len(self.store.view(self.token, 'patient')['documents']), 2)
        self.assertTrue(self.store.document_asset(self.token, 'patient', 'letter', 0).startswith(b'%PDF-'))
        state = self.act('patient', 'ask', questions=['Where is the sheet?', 'When do exercises start?'])
        first, second = [q['id'] for q in state['questions']]
        self.act('patient', 'remind', questions=[first], confirmed=True)
        staff = self.store.view(self.token, 'staff')
        self.assertEqual([len(q['active_reminders']) for q in staff['questions']], [1, 0])
        replies = []
        for q, text in ((first, 'The sheet is now shared.'), (second, 'The specialist will confirm when to begin.')):
            state = self.act('staff', 'draft', text=text, covered=[q])
            replies.append(state['replies'][-1]['id'])
        self.assertEqual(self.store.view(self.token, 'patient')['replies'], [])
        self.act('staff', 'send_batch', reply_ids=replies, recipient_confirmed=True)
        state = self.act('patient', 'follow_up', question_id=second, reply_id=replies[1], text='Should I wait for confirmation?')
        self.assertEqual(len(state['questions']), 2)
        self.assertEqual(len(state['questions'][1]['messages']), 2)
        with self.assertRaises(FlowError):
            self.act('patient', 'acknowledge', question_id=second, reply_id=replies[1])
        state = self.act('staff', 'draft', text='Yes, wait for that confirmation.', covered=[second])
        final = state['replies'][-1]['id']
        self.act('staff', 'send_batch', reply_ids=[final], recipient_confirmed=True)
        state = self.act('patient', 'acknowledge', question_id=second, reply_id=final)
        self.assertEqual(state['questions'][1]['status'], 'resolved')
        self.assertEqual(len(state['replies']), 3)

    def test_invite_second_session_restart_and_delete(self):
        state = self.store.view(self.token, 'staff')
        code = self.store.invite(self.token, state['csrf'], 'patient')['code']
        second, role = self.store.join(code)
        self.assertNotEqual(self.token, second)
        self.assertEqual(role, 'patient')
        with self.assertRaises(FlowError):
            self.store.join(code)
        self.act('patient', 'ask', questions=['Which document should I bring?'])
        reopened = WorkspaceStore(self.path/'cases.db', local_ocr=False)
        self.assertEqual(reopened.view(second, 'patient')['questions'][0]['text'], 'Which document should I bring?')
        reopened.delete(self.token, state['csrf'])
        with self.assertRaises(FlowError):
            reopened.view(second, 'patient')

    def test_question_boundaries_preserve_paragraphs(self):
        questions = ['Where is the sheet?\n\nI have checked my documents.\nPlease explain where to find it.',
                     'When is the appointment?\nI may already have the paper reminder.']
        patient = self.act('patient', 'ask', questions=questions)
        self.assertEqual(len(patient['questions']), 2)
        for view in (patient, self.store.view(self.token, 'staff')):
            self.assertEqual([q['text'] for q in view['questions']], questions)
            self.assertEqual([q['messages'][0]['text'] for q in view['questions']], questions)

    def test_direct_reply_saves_and_delivers_in_one_action(self):
        state = self.act('patient', 'ask', questions=['Where is the sheet?'])
        qid = state['questions'][0]['id']
        self.act('staff', 'reply_and_send', replies=[{'question_id': qid, 'text': 'I will share it here.'}],
                 recipient_confirmed=True)
        patient = self.store.view(self.token, 'patient')
        self.assertEqual(patient['questions'][0]['status'], 'answered')
        self.assertEqual(patient['replies'][0]['text'], 'I will share it here.')

    def test_direct_batch_rejects_all_when_one_reply_is_invalid(self):
        state = self.act('patient', 'ask', questions=['First?', 'Second?'])
        first, second = [q['id'] for q in state['questions']]
        with self.assertRaises(FlowError):
            self.act('staff', 'reply_and_send', replies=[{'question_id': first, 'text': 'First answer'},
                {'question_id': second, 'text': ''}], recipient_confirmed=True)
        state = self.store.view(self.token, 'staff')
        self.assertEqual(state['replies'], [])
        self.assertTrue(all(q['status'] == 'open' for q in state['questions']))
        with self.assertRaises(FlowError):
            self.act('patient', 'reply_and_send', replies=[{'question_id': first, 'text': 'Impersonation'}],
                     recipient_confirmed=True)

    def test_direct_reply_cannot_repurpose_another_question_draft(self):
        state = self.act('patient', 'ask', questions=['First?', 'Second?'])
        first, second = [q['id'] for q in state['questions']]
        state = self.act('staff', 'draft', text='Saved answer', covered=[first])
        draft_id = state['replies'][0]['id']
        with self.assertRaises(FlowError):
            self.act('staff', 'reply_and_send', replies=[{'question_id': second, 'text': 'Changed answer',
                'reply_id': draft_id}], recipient_confirmed=True)
        self.act('staff', 'reply_and_send', replies=[{'question_id': first, 'text': 'Final answer',
            'reply_id': draft_id}], recipient_confirmed=True)
        state = self.store.view(self.token, 'patient')
        self.assertEqual(len(state['replies']), 1)
        self.assertEqual(state['replies'][0]['text'], 'Final answer')
        self.assertEqual(state['questions'][1]['status'], 'open')

    def test_csrf_revision_and_personal_boundaries(self):
        state = self.store.view(self.token, 'staff')
        with self.assertRaises(FlowError):
            self.store.mutate(self.token, 'staff', 'wrong', 'draft', {'revision':state['revision']})
        self.act('patient', 'ask', questions=['Where is the sheet?'])
        with self.assertRaises(FlowError):
            self.store.mutate(self.token, 'staff', state['csrf'], 'draft', {'revision':state['revision']})
        with self.assertRaises(ValueError):
            WorkspaceStore(self.path/'private.db', personal=True, page_transcriber=lambda *args: None)

    def test_actual_scan_review_and_explicit_disabled_mode(self):
        source = (ASSETS/'discharge-scan.pdf').read_bytes()
        extracted, images = extract_local_pdf(source, use_ocr=False)
        self.assertEqual(extracted['pages'][0]['text'], '')
        self.assertTrue(images[0].startswith(b'\x89PNG'))
        state = self.store.view(self.token, 'patient')
        state = self.store.upload(self.token, 'patient', state['csrf'], state['revision'], source, 'Example scan')
        upload = state['uploads'][0]
        self.assertEqual(upload['pages'][0]['method'], 'manual_review')
        self.assertTrue(upload['pages'][0]['warnings'])
        with self.assertRaises(FlowError):
            self.act('patient', 'confirm_upload', upload_id=upload['id'], pages=['Reviewed text'], reviewed=False)

    def test_english_negation_guard(self):
        doc = Document('d', 't', 'p', 'e', '1', 'Do not start before review.', True)
        output = {'document_fingerprint':doc.fingerprint, 'source_quote':doc.text,
                  'plain_language':'Start before review.', 'needs_clarification':False}
        with self.assertRaises(FlowError):
            validate_explanation(doc, doc.text, output)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'budget.db'
        initialize_budget(self.path, total=3, daily=3)
        expires = (datetime.now(timezone.utc)+timedelta(days=1)).date().isoformat()
        self.client = GemmaOCR('test-key', self.path, purpose='public_demo', expires=expires)
        self.quota = {'daily_cost_remaining_usd':1, 'daily_cost_limit_usd':1}
        self.response = {'model':'self-dploy/'+MODEL, 'choices':[{'finish_reason':'stop',
                         'message':{'content':'{"text":"Do not start before review."}'}}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_request_timeout_is_diagnosable_without_logging_source_or_credentials(self):
        with patch('carerelay.gemma_ocr.build_opener') as opener, patch('builtins.print') as emitted:
            opener.return_value.open.side_effect=TimeoutError('sensitive upstream detail')
            with self.assertRaisesRegex(DocumentError,'request timeout'):
                self.client._request('/chat/completions',{'source':'private text'})
        record=json.loads(emitted.call_args.args[0])
        self.assertEqual(record['status'],'timeout')
        self.assertEqual(record['operation'],'inference')
        self.assertNotIn('private text',str(record))
        self.assertNotIn('test-key',str(record))
        self.assertNotIn('sensitive',str(record))

    def test_request_timings_correlate_without_logging_body_or_headers(self):
        class Response(BytesIO):
            status = 200
            headers = {'x-request-id': '12345678-1234-1234-1234-123456789012',
                       'x-correlation-id': 'private patient name', 'set-cookie': 'secret'}
        telemetry = {'stage': 'patient-answer-check', 'attempt_id': 'attempt'}
        with trace_task('task'), patch('carerelay.gemma_ocr.build_opener') as opener, patch('builtins.print') as emitted:
            opener.return_value.open.return_value = Response(b'{"value":"private response"}')
            self.client._request('/chat/completions', {'source': 'private document'}, telemetry=telemetry)
        record = json.loads(emitted.call_args.args[0])
        self.assertEqual((record['task_id'], record['attempt_id']), ('task', 'attempt'))
        self.assertEqual(record['http_status'], 200)
        self.assertEqual(record['phase'], 'complete')
        self.assertGreater(record['response_bytes'], 0)
        self.assertGreaterEqual(record['body_read_seconds'], 0)
        self.assertEqual(set(record['provider_ids']), {'x-request-id'})
        self.assertEqual(telemetry['requests'], [record])
        self.assertNotIn('private', json.dumps(record))
        self.assertNotIn('secret', json.dumps(record))
        self.assertNotIn('test-key', json.dumps(record))
        self.assertIsNone(TASK_ID.get())

    def test_connection_timeout_wrapped_by_urllib_remains_a_timeout(self):
        from urllib.error import URLError
        with patch('carerelay.gemma_ocr.build_opener') as opener, patch('builtins.print') as emitted:
            opener.return_value.open.side_effect = URLError(TimeoutError('sensitive detail'))
            with self.assertRaisesRegex(DocumentError, 'request timeout'):
                self.client._request('/usage?include_recent=false')
        self.assertEqual(json.loads(emitted.call_args.args[0])['status'], 'timeout')

    def test_transport_separates_connection_send_and_headers_without_disabling_tls(self):
        import ssl
        clock = [0.0]
        timings = {}

        def tcp(*args, **kwargs):
            clock[0] += 2
            return object()

        def connect(connection):
            connection.sock = connection._create_connection(('example.invalid', 443))
            clock[0] += 3

        def send(connection, data):
            if connection.sock is None:
                connection.connect()
            clock[0] += .5

        def headers(connection):
            clock[0] += 40
            raise TimeoutError()

        with patch('socket.create_connection', side_effect=tcp), patch('carerelay.api_protocol.time.perf_counter', side_effect=lambda: clock[0]), \
                patch('http.client.HTTPSConnection.connect', connect), patch('http.client.HTTPSConnection.send', send), \
                patch('http.client.HTTPSConnection.getresponse', headers):
            connection = _TimedHTTPSConnection('example.invalid', timings=timings)
            self.assertTrue(connection._context.check_hostname)
            self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
            connection.send(b'private content')
            with self.assertRaises(TimeoutError):
                connection.getresponse()
        self.assertEqual(timings['dns_tcp_seconds'], 2)
        self.assertEqual(timings['tls_tunnel_seconds'], 3)
        self.assertEqual(timings['connection_seconds'], 5)
        self.assertEqual(timings['send_seconds'], .5)
        self.assertEqual(timings['response_headers_seconds'], 40)
        self.assertEqual(timings['phase'], 'response_headers')
        self.assertNotIn('private', json.dumps(timings))

    def test_busy_response_keeps_retry_delay_and_blocks_early_retries(self):
        from io import BytesIO
        from urllib.error import HTTPError
        from carerelay.gemma_ocr import BusyServiceError
        body=b'{"detail":{"error":{"code":"model_concurrency_rate_limit_exceeded","message":"private upstream text"}}}'
        response=HTTPError('https://example.invalid',429,'Limited',{'Retry-After':'60'},BytesIO(body))
        with patch('carerelay.gemma_ocr.build_opener') as opener, patch('builtins.print') as emitted:
            opener.return_value.open.side_effect=response
            with self.assertRaises(BusyServiceError) as raised:
                self.client._request('/chat/completions',{'source':'private document'})
        self.assertGreater(datetime.fromisoformat(raised.exception.retry_after_utc),datetime.now(timezone.utc))
        record=json.loads(emitted.call_args.args[0])
        self.assertEqual(record['retry_after_seconds'],60)
        self.assertEqual(record['limit_code'],'model_concurrency_rate_limit_exceeded')
        self.assertNotIn('private',json.dumps(record))
        with patch.object(self.client,'_request') as call:
            with self.assertRaises(BusyServiceError):
                self.client.chat('test',{},effort='medium',stage='ward-draft')
            call.assert_not_called()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM inference_attempts').fetchone()[0],0)
            self.assertIsNone(db.execute('SELECT active_id FROM inference_lease').fetchone()[0])

    def test_fixed_model_image_only_and_persistent_accounting(self):
        with patch.object(self.client, '_request', side_effect=[self.quota, self.response]) as call:
            result = self.client.transcribe_page(b'\x89PNGexample', 'Reference must not be sent')
        payload = call.call_args.args[1]
        self.assertNotIn('Reference must not be sent', json.dumps(payload))
        self.assertEqual(payload['model'], MODEL)
        self.assertEqual(payload['reasoning_effort'], 'none')
        self.assertEqual(result['text'], 'Do not start before review.')
        initialize_budget(self.path, total=3, daily=3)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM inference_attempts').fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT active_id FROM inference_lease').fetchone()[0])
        with self.assertRaises(ValueError):
            initialize_budget(self.path, total=4, daily=3)

    def test_bad_output_counted_and_failed_quota_sends_no_page(self):
        self.response['choices'][0]['message']['content'] = '{"text":"a","text":"b"}'
        with patch.object(self.client, '_request', side_effect=[self.quota, self.response]) as call:
            with self.assertRaises(DocumentError):
                self.client.transcribe_page(b'\x89PNGexample', '')
            self.assertEqual(call.call_count, 2)
        with patch.object(self.client, '_request', return_value={}) as call, patch('carerelay.gemma_ocr.time.sleep'):
            with self.assertRaises(DocumentError):
                self.client.transcribe_page(b'\x89PNGexample', '')
            self.assertEqual(call.call_count, 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM inference_attempts').fetchone()[0], 1)


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server({'AFTERWARD_HOSTED':'1', 'AFTERWARD_PUBLIC_HOST':'demo.example',
            'AFTERWARD_DATA_DIR':str(Path(self.temp.name)/'data'), 'PORT':'18768'})
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        conn.request(method, path, body=body, headers={'Host':'demo.example', **(headers or {})})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_hosts_health_guides_and_private_paths(self):
        self.assertEqual(self.request('GET', '/health', headers={'Host':'healthcheck.railway.app'})[0], 200)
        self.assertEqual(self.request('GET', '/api/config', headers={'Host':'healthcheck.railway.app'})[0], 403)
        self.assertEqual(self.request('GET', '/', headers={'Host':'evil.example'})[0], 403)
        for path in ('/', '/judge-guide', '/presentation', '/examples/discharge-scan.pdf'):
            self.assertEqual(self.request('GET', path)[0], 200)
        for path in ('/.env', '/data/workspaces.sqlite3', '/examples/../../.env'):
            self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(json.loads(self.request('GET', '/api/config')[2])['ocr_provider'], 'disabled')

    def test_secure_cookie_known_pdf_only_and_csrf(self):
        headers = {'Origin':'https://demo.example', 'X-AfterWard-Request':'1', 'Content-Type':'application/json'}
        status, response, _ = self.request('POST', '/api/start', '{"acknowledged":true}', headers)
        self.assertEqual(status, 201)
        self.assertIn('Secure', response['Set-Cookie'])
        headers['Cookie'] = response['Set-Cookie'].split(';')[0]
        state = json.loads(self.request('GET', '/api/patient', headers=headers)[2])
        source = (ASSETS/'letter.pdf').read_bytes()
        headers.update({'X-AfterWard-CSRF':state['csrf'], 'X-AfterWard-Revision':str(state['revision']), 'Content-Type':'application/pdf'})
        self.assertEqual(self.request('POST', '/api/patient/upload', source+b'changed', headers)[0], 409)
        self.assertEqual(self.request('POST', '/api/patient/upload', source, {**headers, 'Origin':'https://other.example'})[0], 409)
        self.assertEqual(self.request('POST', '/api/patient/upload', source, headers)[0], 200)


if __name__ == '__main__':
    unittest.main()
