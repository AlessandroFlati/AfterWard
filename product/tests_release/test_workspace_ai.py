"""Exercise source validation, asynchronous boundaries and human publication gates."""
from pathlib import Path
from contextlib import closing
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import sqlite3
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from carerelay.discharge_ai import DischargeAI, packet_for, validate_stage
from carerelay.discharge_flow import Document, FlowError, validate_explanation
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

    def test_glossary_uses_medium_and_keeps_exact_context(self):
        term = {'term': 'ECG', 'source_quote': 'An ECG was performed.',
                'plain_language': 'A test that records the electrical activity of the heart.', 'needs_clarification': False}
        client = ScriptedClient([{'terms': [term, term]}])
        result = DischargeAI(client).glossary('Investigations\nAn ECG was performed.')
        self.assertEqual(client.calls[0][:2], ('clinical-glossary', 'medium'))
        self.assertEqual(result['terms'], [term])

    def test_document_question_receives_question_and_full_context(self):
        doc = Document('letter', 'team', 'patient', 'stay', '1',
                       'Paracetamol 500 mg.\nFollow the instructions on the sheet.', True)
        def answer(context):
            return {k: context[k] for k in ('document_fingerprint', 'source_quote')} | {
                'plain_language': 'mg means milligrams, a unit used to measure the amount of medicine.',
                'needs_clarification': False, 'in_scope': True}
        client = ScriptedClient([answer])
        result = DischargeAI(client).explain(doc.__dict__, 'Paracetamol 500 mg.', lambda _: None,
                                            question='What does mg mean here?')
        stage, effort, context = client.calls[0]
        self.assertEqual((stage, effort), ('document-question', 'none'))
        self.assertEqual(context['question'], 'What does mg mean here?')
        self.assertEqual(context['document_text'], doc.text)
        self.assertIn('milligrams', result['explanation']['plain_language'])
        self.assertEqual(result['explanation']['source_quote'], 'Paracetamol 500 mg.')

    def test_document_question_rejects_bad_source_new_numbers_and_echo(self):
        doc = Document('letter', 'team', 'patient', 'stay', '1', 'Paracetamol 500 mg.', True)
        value = {'document_fingerprint': doc.fingerprint, 'source_quote': doc.text,
                 'plain_language': 'mg means milligrams.', 'needs_clarification': False, 'in_scope': True}
        for change in [{'document_fingerprint': 'other'}, {'source_quote': 'Invented extract'},
                       {'plain_language': 'Take 900 mg.'}, {'plain_language': 'Paracetamol 500 mg'},
                       {'in_scope': 'yes'}, {'needs_clarification': 'no'}]:
            with self.subTest(change=change), self.assertRaises(FlowError):
                validate_explanation(doc, doc.text, {**value, **change}, question='What does mg mean?')

    def test_open_question_selects_its_source_from_the_full_document(self):
        doc = Document('letter', 'team', 'patient', 'stay', '1',
                       'Discharge letter\nParacetamol 500 mg.\nContinue outpatient follow-up.', True)
        def answer(context):
            self.assertNotIn('source_quote', context)
            self.assertEqual(context['document_text'], doc.text)
            return {'document_fingerprint': context['document_fingerprint'],
                    'source_quote': 'Paracetamol 500 mg.', 'plain_language': 'mg means milligrams.',
                    'needs_clarification': False, 'in_scope': True}
        client = ScriptedClient([answer])
        result = DischargeAI(client).explain(doc.__dict__, None, lambda _: None,
                                            question='What does mg mean here?')
        self.assertEqual(result['explanation']['source_quote'], 'Paracetamol 500 mg.')
        self.assertEqual(client.calls[0][:2], ('document-question', 'none'))
        self.assertEqual(result['prompt_version'], 'document-question-en-2')

    def test_open_question_rejects_absent_or_ambiguous_automatic_sources(self):
        doc = Document('letter', 'team', 'patient', 'stay', '1',
                       'Paracetamol 500 mg.\nContinue follow-up.\nContinue follow-up.', True)
        value = {'document_fingerprint': doc.fingerprint, 'source_quote': 'Paracetamol 500 mg.',
                 'plain_language': 'mg means milligrams.', 'needs_clarification': False, 'in_scope': True}
        for change in [{'source_quote': ''}, {'source_quote': None}, {'source_quote': 'Invented passage'},
                       {'source_quote': 'Continue follow-up.'}, {'document_fingerprint': 'other'},
                       {'plain_language': 'Take 900 mg.'}, {'plain_language': 'Paracetamol 500 mg'},
                       {'in_scope': 'yes'}, {'needs_clarification': 'no'}]:
            with self.subTest(change=change), self.assertRaises(FlowError):
                validate_explanation(doc, None, {**value, **change}, question='What does mg mean?')

    def test_open_question_allows_no_source_only_when_answer_is_withheld(self):
        doc = Document('letter', 'team', 'patient', 'stay', '1', 'Paracetamol 500 mg.', True)
        for in_scope, unclear in [(False, False), (True, True)]:
            result = validate_explanation(doc, None, {'document_fingerprint': doc.fingerprint,
                'source_quote': '', 'plain_language': 'Unsupported content must not be displayed.',
                'needs_clarification': unclear, 'in_scope': in_scope}, question='A user question')
            self.assertEqual(result['source_quote'], '')
            self.assertEqual(result['plain_language'], '')
            self.assertTrue(result['needs_clarification'])

    def test_document_question_withholds_unrelated_and_uncertain_answers(self):
        doc = Document('letter', 'team', 'patient', 'stay', '1', 'Paracetamol 500 mg.', True)
        for in_scope, unclear in [(False, False), (True, True)]:
            result = validate_explanation(doc, doc.text, {'document_fingerprint': doc.fingerprint,
                'source_quote': doc.text, 'plain_language': 'Unsupported content must not be displayed.',
                'needs_clarification': unclear, 'in_scope': in_scope}, question='A user question')
            self.assertEqual(result['plain_language'], '')
            self.assertTrue(result['needs_clarification'])

    def test_glossary_rejects_invented_source_and_unsupported_numbers(self):
        for quote, plain in [('ECG is normal.', 'A heart test.'), ('ECG was performed.', 'A 5-minute heart test.')]:
            with self.subTest(quote=quote), self.assertRaises(FlowError):
                DischargeAI(ScriptedClient([{'terms': [{'term': 'ECG', 'source_quote': quote,
                    'plain_language': plain, 'needs_clarification': False}]}])).glossary('ECG was performed.')

    def test_glossary_withholds_uncertain_meaning(self):
        result = DischargeAI(ScriptedClient([{'terms': [{'term': 'PT', 'source_quote': 'PT requested.',
            'plain_language': 'A guess must not be shown.', 'needs_clarification': True}]}])).glossary('PT requested.')
        self.assertEqual(result['terms'][0]['plain_language'], '')

    def test_glossary_restores_source_line_break_without_changing_words(self):
        quote = 'PARTIAL THROMBOPLASTIN TIME, GLUCOSE'
        row = {'term': 'PARTIAL THROMBOPLASTIN TIME', 'source_quote': quote,
               'plain_language': 'A blood clotting test.', 'needs_clarification': False}
        source = 'PARTIAL THROMBOPLASTIN TIME,\nGLUCOSE'
        result = DischargeAI(ScriptedClient([{'terms': [row]}])).glossary(source)
        self.assertEqual(result['terms'][0]['source_quote'], source)
        for invalid in [source+'\n'+quote, 'PARTIAL THROMBOPLASTIN TIME, NO GLUCOSE',
                        'PARTIAL THROMBOPLASTIN TIME; GLUCOSE']:
            with self.subTest(source=invalid), self.assertRaises(FlowError):
                DischargeAI(ScriptedClient([{'terms': [row]}])).glossary(invalid)

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

    def test_patient_answer_check_returns_source_excerpts_and_medium(self):
        client = ScriptedClient([{'answers': [{'question_id': 'q1', 'status': 'found',
            'citations': [{'source_id': 'p1l1'}]}, {'question_id': 'q2', 'status': 'not_found', 'citations': []}]}])
        result = DischargeAI(client).answer(self.docs, [{'id': 'q1', 'text': 'Where are the exercises?'},
                                                      {'id': 'q2', 'text': 'When is my appointment?'}], lambda _: None)
        self.assertEqual(client.calls[0][1], 'medium')
        self.assertEqual(result['answers'][0]['citations'][0]['quote'], self.docs[0]['pages'][0])
        self.assertNotIn('text', result['answers'][0])

    def test_patient_answer_check_rejects_missing_evidence_and_wrong_questions(self):
        cases = [
            {'question_id': 'q1', 'status': 'found', 'citations': []},
            {'question_id': 'q2', 'status': 'not_found', 'citations': []},
            {'question_id': 'q1', 'status': 'conflicting', 'citations': [{'source_id': 'p1l1'}]},
            {'question_id': 'q1', 'status': 'not_found', 'citations': [{'source_id': 'p1l1'}]},
        ]
        for row in cases:
            with self.subTest(row=row), self.assertRaises(FlowError):
                DischargeAI(ScriptedClient([{'answers': [row]}])).answer(self.docs,
                    [{'id': 'q1', 'text': 'Question'}], lambda _: None)

    def test_patient_answer_check_rejects_nonexistent_quote(self):
        client = ScriptedClient([{'answers': [{'question_id': 'q1', 'status': 'found',
                                             'citations': [{'source_id': 'p1l999'}]}]}])
        with self.assertRaises((FlowError, ValueError)):
            DischargeAI(client).answer(self.docs, [{'id': 'q1', 'text': 'Question'}], lambda _: None)

    def test_ward_draft_recommends_cited_undelivered_document(self):
        docs = self.docs + [{'identifier': 'rehab', 'title': 'Rehabilitation programme',
                            'fingerprint': 'r1', 'delivered': False, 'pages': ['Rehabilitation programme: supplied exercises.']}]
        value = {'text': 'The rehabilitation programme is in the ward case file.',
                 'needs_clarification': False, 'citations': [{'source_id': 'p2l1'}],
                 'share_documents': ['rehab']}
        client = ScriptedClient([value])
        result = DischargeAI(client).draft(docs, {'text': 'Please send the sheet.'}, lambda _: None)
        self.assertEqual(result['share_documents'], ['rehab'])
        self.assertEqual(result['citations'][0]['document_id'], 'rehab')
        self.assertEqual(client.calls[0][:2], ('ward-draft', 'medium'))
        self.assertEqual(client.calls[0][2]['delivery'][1],
                         {'document_id': 'rehab', 'title': 'Rehabilitation programme', 'patient_available': False})

    def test_ward_draft_rejects_unknown_delivered_uncited_or_duplicate_shares(self):
        for ids, citations in [(['other'], [{'source_id': 'p1l1'}]),
                              (['letter'], [{'source_id': 'p1l1'}]),
                              (['rehab'], [{'source_id': 'p1l1'}]),
                              (['rehab', 'rehab'], [{'source_id': 'p2l1'}])]:
            docs = self.docs + [{'identifier': 'rehab', 'title': 'Exercises', 'fingerprint': 'r1',
                                'delivered': False, 'pages': ['Exercise sheet.']}]
            value = {'text': 'A proposed reply.', 'needs_clarification': False,
                     'citations': citations, 'share_documents': ids}
            with self.subTest(ids=ids, citations=citations), self.assertRaises(FlowError):
                DischargeAI(ScriptedClient([value])).draft(docs, {'text': 'Please send the sheet.'}, lambda _: None)

    def test_ward_draft_can_acknowledge_absent_sheet_without_proposing_share(self):
        value = {'text': 'The sheet is not in this packet; staff need to obtain it.',
                 'needs_clarification': True, 'citations': [], 'share_documents': []}
        result = DischargeAI(ScriptedClient([value])).draft(self.docs,
                     {'text': 'Please send the exercises.'}, lambda _: None)
        self.assertEqual(result['share_documents'], [])
        self.assertTrue(result['needs_clarification'])

    def test_ward_draft_can_point_to_delivered_sheet_with_source(self):
        docs = self.docs + [{'identifier': 'rehab', 'title': 'Exercises', 'fingerprint': 'r1',
                            'delivered': True, 'pages': ['Exercise sheet.']}]
        value = {'text': 'The exercise sheet is already available to you.',
                 'needs_clarification': False, 'citations': [{'source_id': 'p2l1'}],
                 'share_documents': []}
        result = DischargeAI(ScriptedClient([value])).draft(docs,
                     {'text': 'Please send the exercises.'}, lambda _: None)
        self.assertEqual(result['share_documents'], [])
        self.assertEqual(result['citations'][0]['document_id'], 'rehab')
        with self.assertRaisesRegex(FlowError, 'unsupported factual reply'):
            DischargeAI(ScriptedClient([{**value, 'citations': []}])).draft(docs,
                         {'text': 'Please send the exercises.'}, lambda _: None)


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
        self.store = WorkspaceStore(Path(self.temp.name)/'cases.db', ai=self.ai, local_ocr=False, precompute_glossary=False)
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

    def test_retry_time_survives_job_persistence_and_view_filtering(self):
        from carerelay.gemma_ocr import BusyServiceError
        error=BusyServiceError(time.time()+60)
        with patch.object(self.ai,'analyze',side_effect=error):
            self.start()
            job=self.finish()
        self.assertEqual(job['status'],'failed')
        self.assertEqual(job['retry_after_utc'],error.retry_after_utc)
        self.assertEqual(self.store.view(self.token,'patient')['ai_jobs'][-1]['retry_after_utc'],error.retry_after_utc)
        self.assertEqual(self.store.view(self.token,'staff')['ai_jobs'],[])

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

    def test_explanation_visible_without_staff_review_and_review_is_optional(self):
        def explanation(context):
            return {**{k: context[k] for k in ('document_fingerprint', 'source_quote')},
                    'plain_language': 'Keep attending later appointments at the outpatient clinic.', 'needs_clarification': False}
        self.store.ai = DischargeAI(ScriptedClient([explanation]))
        self.start(kind='explain', quote=LETTER.splitlines()[0])
        job = self.finish()
        self.assertEqual(job['status'], 'completed')
        self.assertNotIn('explanation', job['result'])
        visible = self.store.view(self.token, 'patient')['explanations']
        self.assertEqual(len(visible), 1)
        self.assertFalse(visible[0]['approved'])
        self.assertIn('outpatient clinic', visible[0]['plain_language'])
        self.assertIn('started_at_utc', job)
        self.assertIn('finished_at_utc', job)
        state = self.store.view(self.token, 'staff')
        exp = state['explanations'][0]
        self.store.mutate(self.token, 'staff', state['csrf'], 'approve_explanation',
                          {'revision': state['revision'], 'explanation_id': exp['id']})
        self.assertTrue(self.store.view(self.token, 'patient')['explanations'][0]['approved'])

    def test_document_question_is_saved_with_answer_without_contacting_ward(self):
        def explanation(context):
            self.assertNotIn('source_quote', context)
            return {'document_fingerprint': context['document_fingerprint'],
                    'source_quote': LETTER.splitlines()[0],
                    'plain_language': 'An outpatient clinic provides care without an overnight hospital stay.',
                    'needs_clarification': False, 'in_scope': True}
        client = ScriptedClient([explanation])
        self.store.ai = DischargeAI(client)
        self.start(kind='explain', question='What is an outpatient clinic?')
        self.assertEqual(self.finish()['status'], 'completed')
        reloaded = WorkspaceStore(self.store.path, local_ocr=False, precompute_glossary=False)
        state = reloaded.view(self.token, 'patient')
        self.assertEqual(state['questions'], [])
        self.assertEqual(state['explanations'][0]['question'], 'What is an outpatient clinic?')
        self.assertTrue(state['explanations'][0]['in_scope'])
        self.assertEqual(state['explanations'][0]['source_quote'], LETTER.splitlines()[0])
        self.assertIn('overnight', state['explanations'][0]['plain_language'])

    def test_document_question_requires_accessible_document_and_valid_input_before_inference(self):
        client = ScriptedClient([])
        self.store.ai = DischargeAI(client)
        for args in [{'quote': 'Not present in this letter', 'question': 'Explain this'},
                     {'quote': LETTER.splitlines()[0], 'question': ''},
                     {'quote': LETTER.splitlines()[0], 'question': 'x' * 2001},
                     {'documents': ['rehab'], 'question': 'Explain this'},
                     {'question': ''}, {'question': 'x' * 2001}, {}]:
            with self.subTest(args=args), self.assertRaises(FlowError):
                self.start(kind='explain', **args)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.view(self.token, 'patient')['ai_jobs'], [])

    def test_uncertain_explanation_does_not_publish_generated_text_or_send_question(self):
        def explanation(context):
            return {**{k:context[k] for k in ('document_fingerprint', 'source_quote')},
                    'plain_language': 'Clinical context is needed to interpret this passage.', 'needs_clarification': True}
        self.store.ai = DischargeAI(ScriptedClient([explanation]))
        self.start(kind='explain', quote=LETTER.splitlines()[0])
        self.assertEqual(self.finish()['status'], 'completed')
        state = self.store.view(self.token, 'patient')
        self.assertTrue(state['explanations'][0]['needs_clarification'])
        self.assertEqual(state['explanations'][0]['plain_language'], '')
        self.assertEqual(state['questions'], [])
        staff = self.store.view(self.token, 'staff')
        self.assertIn('Clinical context', staff['explanations'][0]['plain_language'])

    def test_explanation_of_undelivered_document_stays_private(self):
        def explanation(context):
            return {**{k:context[k] for k in ('document_fingerprint', 'source_quote')},
                    'plain_language': context['source_quote'], 'needs_clarification': False}
        self.store.ai = DischargeAI(ScriptedClient([explanation]))
        source=next(d for d in self.store.view(self.token,'staff')['documents'] if d['identifier']=='rehab')
        self.start('staff',kind='explain',documents=['rehab'],quote=source['text'].splitlines()[0])
        self.assertEqual(self.finish('staff')['status'],'completed')
        self.assertEqual(self.store.view(self.token,'patient')['explanations'],[])

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

    def start_answer_check(self):
        self.store.ai = DischargeAI(ScriptedClient([{'answers': [
            {'question_id': 'q1', 'status': 'found', 'citations': [{'source_id': 'p1l1'}]},
            {'question_id': 'q2', 'status': 'not_found', 'citations': []}]}]))
        self.start(kind='answer', questions=['What is follow-up?', 'What time is my appointment?'])
        job = self.finish()
        self.assertEqual(job['status'], 'completed')
        return job

    def test_answer_check_is_private_until_patient_sends_a_selection(self):
        job = self.start_answer_check()
        staff = self.store.view(self.token, 'staff')
        self.assertEqual(staff['questions'], [])
        self.assertEqual(staff['ai_jobs'], [])
        state = self.store.view(self.token, 'patient')
        state = self.store.mutate(self.token, 'patient', state['csrf'], 'ask_checked',
            {'revision': state['revision'], 'job_id': job['id'], 'keys': ['q2']})
        self.assertEqual([q['text'] for q in state['questions']], ['What time is my appointment?'])
        with self.assertRaisesRegex(FlowError, 'already has'):
            self.store.mutate(self.token, 'patient', state['csrf'], 'ask_checked',
                {'revision': state['revision'], 'job_id': job['id'], 'keys': ['q2']})
        state = self.store.mutate(self.token, 'patient', state['csrf'], 'ask_checked',
            {'revision': state['revision'], 'job_id': job['id'], 'keys': ['q1']})
        self.assertEqual(len(state['questions']), 2)

    def test_answer_check_limits_sources_and_rejects_changed_documents(self):
        with self.assertRaisesRegex(FlowError, 'available'):
            self.start(kind='answer', documents=['rehab'], questions=['What exercises?'])
        with self.assertRaisesRegex(FlowError, 'available AI action'):
            self.start(role='staff', kind='answer', questions=['Question'])
        job = self.start_answer_check()
        state = self.store.view(self.token, 'patient')
        state = self.store.mutate(self.token, 'patient', state['csrf'], 'supplement',
            {'revision': state['revision'], 'fixture': 'rehab'})
        self.assertTrue(state['ai_jobs'][-1]['stale'])
        with self.assertRaisesRegex(FlowError, 'current documents'):
            self.store.mutate(self.token, 'patient', state['csrf'], 'ask_checked',
                {'revision': state['revision'], 'job_id': job['id'], 'keys': ['q2']})


class GlossaryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'cache.db'
        self.client = ScriptedClient([{'terms': []}] * 15)
        self.store = WorkspaceStore(self.path, ai=DischargeAI(self.client), local_ocr=False)
        self.token = self.store.create('hospital')

    def tearDown(self):
        self.wait_idle()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic()+4
        while self.store.glossary_running and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertFalse(self.store.glossary_running)

    def test_shared_cache_survives_restart_without_another_call(self):
        self.store.view(self.token, 'patient')
        other = self.store.create('hospital')
        self.store.view(other, 'patient')
        self.wait_idle()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.store.view(other, 'patient')['glossaries']['letter']['status'], 'completed')
        self.store = WorkspaceStore(self.path, ai=DischargeAI(self.client), local_ocr=False)
        self.store.view(other, 'patient')
        self.wait_idle()
        self.assertEqual(len(self.client.calls), 1)

    def test_ward_cache_does_not_expose_undelivered_documents(self):
        self.store.view(self.token, 'staff')
        self.wait_idle()
        patient = self.store.view(self.token, 'patient')
        self.assertEqual(set(patient['glossaries']), {'letter'})
        self.assertEqual(set(self.store.view(self.token, 'staff')['glossaries']), {'letter', 'rehab', 'appointment'})

    def test_changed_text_and_prompt_cannot_reuse_old_result(self):
        original = self.store._glossary_key('PT requested.')
        self.assertNotEqual(original, self.store._glossary_key('PT requested after rehabilitation.'))
        with patch('carerelay.discharge_service.GLOSSARY_VERSION', 'next-version'):
            self.assertNotEqual(original, self.store._glossary_key('PT requested.'))

    def test_starts_after_last_ocr_page_and_recomputes_corrected_text(self):
        self.token = self.store.create('patient')
        self.store.page_transcriber = lambda image, reference: {'text': reference, 'provider': 'amd_api'}
        pages = [{'number': i, 'text': '', 'original_text': text, 'method': 'gemma_pending', 'warnings': []}
                 for i, text in enumerate(['ECG was performed.', 'Continue follow-up.'], 1)]
        with closing(sqlite3.connect(self.path)) as db, db:
            csrf, record = self.store._load(db, self.token)
            record['uploads'] = [{'id': 'scan', 'title': 'Scan', 'uploader_role': 'patient', 'confirmed': False, 'pages': pages}]
            db.execute('UPDATE workspaces SET record=? WHERE token_hash=?', (json.dumps(record), self.store.digest(self.token)))
            db.executemany('INSERT INTO upload_assets VALUES (?,?,?,?)',
                [(self.store.digest(self.token), 'scan', i, b'test') for i in (1, 2)])
        state = self.store.view(self.token, 'patient')
        state = self.store.read_page(self.token, 'patient', csrf, state['revision'], 'scan', 1)
        self.assertFalse(self.store.glossary_running)
        state = self.store.read_page(self.token, 'patient', csrf, state['revision'], 'scan', 2)
        self.wait_idle()
        self.assertEqual(len(self.client.calls), 1)
        state = self.store.mutate(self.token, 'patient', csrf, 'confirm_upload', {'revision': state['revision'],
            'upload_id': 'scan', 'reviewed': True, 'pages': ['ECG was performed.', 'Continue outpatient follow-up.']})
        self.wait_idle()
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.store.view(self.token, 'patient')['glossaries']['pdf-scan']['status'], 'completed')

    def test_failed_generation_is_not_repeated_by_polling(self):
        self.client.responses = iter([{'invalid': True}])
        self.store.view(self.token, 'patient')
        self.wait_idle()
        state = self.store.view(self.token, 'patient')
        self.assertEqual(state['glossaries']['letter']['status'], 'failed')
        self.store.view(self.token, 'patient')
        self.assertEqual(len(self.client.calls), 1)
        self.client.responses = iter([{'terms': []}])
        self.store.mutate(self.token, 'patient', state['csrf'], 'retry_glossary',
            {'revision': state['revision'], 'document_id': 'letter'})
        self.wait_idle()
        self.assertEqual(self.store.view(self.token, 'patient')['glossaries']['letter']['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
