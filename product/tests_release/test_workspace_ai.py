"""Exercise source validation, asynchronous boundaries and human publication gates."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from carerelay.discharge_ai import DischargeAI, packet_for, validate_stage
from carerelay.discharge_flow import FlowError
from carerelay.discharge_service import WorkspaceStore, LETTER


class ScriptedClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, prompt, context, *, effort, stage):
        self.calls.append((stage, effort, context))
        value = next(self.responses)
        if callable(value):
            value = value(context)
        return value, {'model': 'Gemma-4-31B-IT', 'stage': stage, 'reasoning_effort': effort,
                       'elapsed_seconds': 0, 'usage': {}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.docs = [{'identifier': 'letter', 'title': 'Letter', 'fingerprint': 'x', 'delivered': True,
                      'pages': ['Follow the exercises on the supplied sheet.']}]
        self.fact = {'topic': 'rehabilitation', 'field': 'required_document', 'citations': [{'source_id': 'p1l1'}]}
        self.finding = {'topic': 'rehabilitation', 'code': 'missing_attachment', 'citations': [{'source_id': 'p1l1'}]}

    def test_two_pass_medium_and_exact_source(self):
        client = ScriptedClient([{'facts': [self.fact]}, {'findings': [self.finding], 'fact_checks': []}])
        result = DischargeAI(client).analyze(self.docs, lambda _: None)
        self.assertEqual([c[1] for c in client.calls], ['medium', 'medium'])
        citation = result['findings'][0]['citations'][0]
        self.assertEqual(citation['quote'], self.docs[0]['pages'][0])
        self.assertEqual(citation['document_id'], 'letter')
        self.assertEqual(citation['document_page'], 1)

    def test_one_correction_cannot_silently_accept_invalid_citations(self):
        bad = {**self.finding, 'citations': [{'source_id': 'p1l999'}]}
        client = ScriptedClient([{'facts': []}, {'findings': [bad], 'fact_checks': []}, {'findings': [bad], 'fact_checks': []}])
        with self.assertRaisesRegex(FlowError, 'still failed'):
            DischargeAI(client).analyze(self.docs, lambda _: None)
        self.assertEqual(len(client.calls), 3)

    def test_contrary_fact_requires_explicit_audit(self):
        packet, _ = packet_for(self.docs)
        fact = {'topic': 'rehabilitation', 'field': 'document_content',
                'citations': [{'page': 1, 'quote': self.docs[0]['pages'][0]}]}
        with self.assertRaisesRegex(ValueError, 'Unresolved contradiction'):
            validate_stage({'findings': [self.finding], 'fact_checks': []}, packet, 'findings', [fact])

    def test_conflict_stage_is_conditional_and_cannot_remove_findings(self):
        self.docs[0]['pages'] = ['Take one tablet.\nTake two tablets.']
        facts = [{'topic': 'medicine', 'field': 'amount', 'citations': [{'source_id': 'p1l'+str(i)}]} for i in (1, 2)]
        conflict = {'topic': 'medicine', 'code': 'conflict', 'citations': [{'source_id': 'p1l1'}, {'source_id': 'p1l2'}]}
        pair = {'pair_id': 'P01', 'decision': 'conflict', 'explanation': 'The same task has incompatible quantities.', 'citations': conflict['citations']}
        client = ScriptedClient([{'facts': facts}, {'findings': [], 'fact_checks': []},
            {'findings': [conflict], 'fact_checks': [], 'pair_checks': [pair], 'previous_checks': []}])
        result = DischargeAI(client).analyze(self.docs, lambda _: None)
        self.assertEqual(result['findings'][0]['key'], 'medicine:conflict')
        self.assertEqual(len(client.calls), 3)


class BlockingAI:
    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()
        self.documents = None

    def analyze(self, documents, progress):
        self.documents = documents
        progress('Reading documents')
        self.started.set()
        self.release.wait(5)
        return {'findings': [], 'calls': [], 'prompt_version': 'test-only'}


class WorkspaceAITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ai = BlockingAI()
        self.store = WorkspaceStore(Path(self.temp.name)/'cases.db', ai=self.ai, local_ocr=False)
        self.token = self.store.create('hospital')

    def tearDown(self):
        self.ai.release.set()
        deadline = time.monotonic()+6
        while self.store.ai_workers and time.monotonic()<deadline:
            time.sleep(.01)
        self.temp.cleanup()

    def start(self, role='patient', **args):
        state = self.store.view(self.token, role)
        return self.store.start_ai(self.token, role, state['csrf'], {'revision': state['revision'], 'kind': 'analyze', 'documents': ['letter'], **args})

    def finish(self, role='patient'):
        self.ai.release.set()
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            state = self.store.view(self.token, role)
            if not state['ai_pending']:
                return state['ai_jobs'][-1]
            time.sleep(.01)
        self.fail('Worker did not finish')

    def test_patient_cannot_analyze_undelivered_documents(self):
        with self.assertRaisesRegex(FlowError, 'available'):
            self.start(documents=['rehab'])
        self.start()
        self.assertTrue(self.ai.started.wait(2))
        self.assertEqual([d['identifier'] for d in self.ai.documents], ['letter'])
        self.assertEqual(self.finish()['status'], 'completed')
        self.assertEqual(self.store.view(self.token, 'staff')['ai_jobs'], [])

    def test_changed_documents_invalidate_running_check(self):
        self.start()
        self.assertTrue(self.ai.started.wait(2))
        state = self.store.view(self.token, 'patient')
        self.store.mutate(self.token, 'patient', state['csrf'], 'supplement', {'revision': state['revision'], 'fixture': 'rehab'})
        job = self.finish()
        self.assertEqual(job['status'], 'failed')
        self.assertNotIn('result', job)

    def test_double_start_and_deleted_workspace_do_not_reappear(self):
        self.start()
        self.assertTrue(self.ai.started.wait(2))
        with self.assertRaisesRegex(FlowError, 'already running'):
            self.start()
        state = self.store.view(self.token, 'patient')
        self.store.delete(self.token, state['csrf'])
        self.ai.release.set()
        deadline = time.monotonic()+3
        while self.store.ai_workers and time.monotonic()<deadline:
            time.sleep(.01)
        with self.assertRaises(FlowError):
            self.store.view(self.token, 'patient')

    def test_explanation_waits_for_staff_review(self):
        def explanation(context):
            return {**{k: context[k] for k in ('document_fingerprint', 'source_quote')},
                    'plain_language': 'Keep attending later appointments at the outpatient clinic.', 'needs_clarification': False}
        self.store.ai = DischargeAI(ScriptedClient([explanation]))
        self.start(kind='explain', quote=LETTER.splitlines()[0])
        job = self.finish()
        self.assertEqual(job['status'], 'completed')
        self.assertNotIn('explanation', job['result'])
        self.assertEqual(self.store.view(self.token, 'patient')['explanations'], [])
        state = self.store.view(self.token, 'staff')
        exp = state['explanations'][0]
        self.store.mutate(self.token, 'staff', state['csrf'], 'approve_explanation',
                          {'revision': state['revision'], 'explanation_id': exp['id']})
        self.assertTrue(self.store.view(self.token, 'patient')['explanations'][0]['approved'])

    def test_pdf_route_starts_without_unrelated_ward_fixtures(self):
        token = self.store.create('patient')
        self.assertEqual(self.store.view(token, 'staff')['documents'], [])

    def test_personal_workspace_rejects_ai_adapter(self):
        with self.assertRaises(ValueError):
            WorkspaceStore(Path(self.temp.name)/'private.db', personal=True, ai=self.ai)

    def test_ai_progress_does_not_make_conversation_forms_stale(self):
        self.start()
        self.assertTrue(self.ai.started.wait(2))
        state = self.store.view(self.token, 'patient')
        self.finish()
        current = self.store.view(self.token, 'patient')
        self.assertEqual(current['revision'], state['revision'])
        self.assertGreater(current['ai_revision'], state['ai_revision'])
        self.store.mutate(self.token, 'patient', state['csrf'], 'ask',
                          {'revision': state['revision'], 'questions': ['Where can I find the sheet?']})

    def test_selected_finding_keeps_source_and_cannot_be_sent_twice(self):
        client = ScriptedClient([{'facts': []}, {'findings': [{'topic':'rehabilitation', 'code':'missing_attachment',
            'citations':[{'source_id':'p1l3'}]}], 'fact_checks':[]}])
        self.store.ai = DischargeAI(client)
        self.start()
        job = self.finish()
        self.assertEqual(job['status'], 'completed')
        state = self.store.view(self.token, 'patient')
        payload = {'revision':state['revision'], 'job_id':job['id'], 'keys':['rehabilitation:missing_attachment']}
        state = self.store.mutate(self.token, 'patient', state['csrf'], 'ask_findings', payload)
        self.assertEqual(len(state['questions']), 1)
        self.assertNotIn('Source:', state['questions'][0]['text'])
        self.assertEqual(state['questions'][0]['source_finding']['citations'][0]['document_id'], 'letter')
        with self.assertRaisesRegex(FlowError, 'already has'):
            self.store.mutate(self.token, 'patient', state['csrf'], 'ask_findings', {**payload,'revision':state['revision']})


if __name__ == '__main__':
    unittest.main()
