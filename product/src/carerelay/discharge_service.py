"""Durable demonstration workspaces and isolated local personal PDF workspaces.

The fixed identity adapter is deliberately limited to synthetic demonstration
accounts. Personal mode has only the local owner, no staff or delegate identities.
Neither mode is a hospital SSO implementation; neither accepts client actors.
"""
from contextlib import closing
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import uuid

from .discharge_flow import Actor, Document, DischargeFlow, FlowError, bounded_text
from .discharge_ai import GLOSSARY_VERSION, GLOSSARY_MODEL
from .api_protocol import trace_task

TITLES = {'letter': 'Discharge letter', 'rehab': 'Rehabilitation programme',
          'appointment': 'Follow-up appointment reminder', 'personal-sheet': 'Hand-delivered sheet'}
LETTER = ('Continue outpatient follow-up.\n'
          'Follow-up means later appointments at the outpatient clinic.\n\n'
          'Follow the rehabilitation programme on the sheet provided.\n'
          'Refer to the reminder for your follow-up appointment.\n')
REHAB = ('Exercises start after the rehabilitation medicine appointment on 2 December. The specialist will confirm when to begin.\n\n'
         'Programme for this demonstration case: while seated, move each foot up and down 5 times. '
         'Repeat this session once daily for 7 days after the specialist confirms the start.\n'
         'This sheet contains the complete written sequence for the example programme.')
APPOINTMENT = 'Follow-up appointment on 15 December at 09:30, Medicine outpatient clinic.'


def actors(flow):
    if flow.tenant == 'private-local':
        return {'patient': Actor('You', flow.tenant, 'patient', patient_id=flow.patient_id)}
    return {
        'patient': Actor('Giorgio Rinaldi', flow.tenant, 'patient', patient_id=flow.patient_id),
        'delegate': Actor('Sofia Rinaldi', flow.tenant, 'delegate'),
        'staff': Actor('Marta Ricci', flow.tenant, 'staff', episodes=frozenset({flow.episode_id}),
            permissions=frozenset({'read','upload','deliver_documents','reply','explain'}))}


def encode(flow):
    value = {k: v for k, v in flow.__dict__.items() if k != 'lock'}
    value['documents'] = {k: asdict(v) for k, v in flow.documents.items()}
    value['delivered'] = {k: asdict(v) for k, v in flow.delivered.items()}
    value['document_history'] = {k: [asdict(d) for d in rows] for k, rows in flow.document_history.items()}
    value['personal_documents'] = sorted(flow.personal_documents)
    value['delegates'] = {k: v.isoformat() for k, v in flow.delegates.items()}
    value['reminder_interval'] = flow.reminder_interval.total_seconds()
    return value


def decode(value):
    flow = DischargeFlow(tenant=value['tenant'], patient_id=value['patient_id'], episode_id=value['episode_id'],
        entry_route=value['entry_route'], reminder_interval=timedelta(seconds=value['reminder_interval']))
    for key in ('revision','questions','reminders','replies','reply_history','explanations','request_closed','events'):
        setattr(flow, key, value[key])
    flow.documents = {k: Document(**v) for k, v in value['documents'].items()}
    flow.delivered = {k: Document(**v) for k, v in value['delivered'].items()}
    flow.document_history = {k: [Document(**d) for d in rows] for k, rows in value['document_history'].items()}
    flow.personal_documents = set(value['personal_documents'])
    flow.delegates = {k: datetime.fromisoformat(v) for k, v in value['delegates'].items()}
    for question in flow.questions.values():
        question.setdefault('generation', 1)
        question.setdefault('resolved_at_utc', None)
        question.setdefault('messages', [{'id': question['id'], 'text': question['text'],
            'author': question['author'], 'at_utc': None, 'reply_to': None}])
    # Historical staff closure never implies that the patient was satisfied.
    flow.request_closed = bool(flow.questions) and all(q['status'] == 'resolved' for q in flow.questions.values())
    return flow


class WorkspaceStore:
    def __init__(self, path, *, personal=False, page_transcriber=None, allowed_pdf_digests=None, local_ocr=True, ai=None, precompute_glossary=True):
        if personal and (page_transcriber or ai):
            raise ValueError('The shared AMD API cannot process personal documents.')
        self.path = Path(path)
        self.personal = personal
        self.page_transcriber = page_transcriber
        self.allowed_pdf_digests = allowed_pdf_digests
        self.local_ocr = local_ocr
        self.ai = ai
        self.ai_slot = threading.BoundedSemaphore(1)
        self.ai_admission = threading.Lock()
        self.ai_workers = set()
        self.precompute_glossary = bool(precompute_glossary and ai and not personal)
        self.glossary_lock = threading.Lock()
        self.glossary_queue = deque()
        self.glossary_running = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS workspaces (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS upload_assets (workspace TEXT NOT NULL, upload TEXT NOT NULL, page INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY(workspace,upload,page))')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_sessions (token_hash TEXT PRIMARY KEY, workspace TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_invites (code_hash TEXT PRIMARY KEY, workspace TEXT NOT NULL, expires TEXT NOT NULL, role TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS glossary_cache (cache_key TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at_utc TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute("UPDATE glossary_cache SET status='interrupted' WHERE status IN ('queued','running')")
            db.execute('DELETE FROM glossary_cache WHERE updated_at_utc<?',
                ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),))
            self._purge_expired(db)
            for key, raw in db.execute('SELECT token_hash,record FROM workspaces').fetchall():
                record = json.loads(raw)
                interrupted = [j for j in record.get('ai_jobs', []) if j['status'] in ('queued', 'running')]
                for job in interrupted:
                    job.update(status='failed', message='The service restarted during the check. Start it again; no result was accepted.')
                if interrupted:
                    record['ai_revision'] = record.get('ai_revision', 0) + 1
                    db.execute('UPDATE workspaces SET record=? WHERE token_hash=?', (json.dumps(record), key))

    def _purge_expired(self, db):
        db.execute('PRAGMA secure_delete=ON')
        now = datetime.now(timezone.utc).isoformat()
        db.execute('DELETE FROM upload_assets WHERE workspace IN (SELECT token_hash FROM workspaces WHERE expires<=?)', (now,))
        db.execute('DELETE FROM workspace_sessions WHERE workspace IN (SELECT token_hash FROM workspaces WHERE expires<=?)', (now,))
        db.execute('DELETE FROM workspace_invites WHERE expires<=? OR workspace IN (SELECT token_hash FROM workspaces WHERE expires<=?)', (now, now))
        db.execute('DELETE FROM workspaces WHERE expires<=?', (now,))

    def create(self, route='hospital'):
        if route not in ('hospital','patient','before_delivery'):
            raise FlowError('Unknown entry route.')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if self.personal:
            route = 'patient'
        flow = DischargeFlow(tenant='private-local' if self.personal else 'demonstration',
            patient_id='local-owner' if self.personal else 'person-1187',
            episode_id=secrets.token_hex(8) if self.personal else 'STAY-24-1187',
            entry_route='patient' if route == 'patient' else 'hospital')
        identity = actors(flow)
        if not self.personal and route != 'patient':
            for key, text in (('letter', LETTER), ('rehab', REHAB), ('appointment', APPOINTMENT)):
                flow.add_document(identity['staff'], Document(key, flow.tenant, flow.patient_id, flow.episode_id, '1', text, True), flow.revision)
        if route == 'hospital':
            flow.deliver_documents(identity['staff'], ['letter'], flow.revision)
        if not self.personal:
            flow.set_delegate(identity['patient'], 'Sofia Rinaldi', True, flow.revision,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7))
        record = {'schema': 1, 'flow': encode(flow), 'messages': [], 'acknowledgements': [],
            'bundle_confirmation': None, 'intake': [], 'owner': None if self.personal else 'Marta Ricci', 'tasks': [],
            'person': 'PDF case' if route == 'patient' else 'Giorgio Rinaldi'}
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._purge_expired(db)
            if db.execute('SELECT COUNT(*) FROM workspaces WHERE expires>?', (datetime.now(timezone.utc).isoformat(),)).fetchone()[0] >= 100:
                raise FlowError('Workspace limit reached.')
            db.execute('INSERT INTO workspaces VALUES (?,?,?,?)', (self.digest(token), csrf,
                (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(), json.dumps(record, ensure_ascii=False)))
        return token

    @staticmethod
    def digest(token):
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise FlowError('Open a demonstration workspace first.')
        return hashlib.sha256(token.encode()).hexdigest()

    def _workspace(self, db, token):
        key = self.digest(token)
        row = db.execute('SELECT workspace FROM workspace_sessions WHERE token_hash=?', (key,)).fetchone()
        return row[0] if row else key

    def _load(self, db, token):
        row = db.execute('SELECT csrf,expires,record FROM workspaces WHERE token_hash=?', (self._workspace(db, token),)).fetchone()
        if not row or datetime.fromisoformat(row[1]) <= datetime.now(timezone.utc):
            raise FlowError('This workspace has expired. Open a new demonstration.')
        record = json.loads(row[2])
        record['workspace_code'] = self._workspace(db, token)[:8].upper()
        if (record['flow']['tenant'] == 'private-local') != self.personal:
            raise FlowError('Access denied.')
        return row[0], record

    def _actor(self, flow, role):
        if self.personal and role != 'patient':
            raise FlowError('Access denied.')
        identity = actors(flow)
        if role not in identity:
            raise FlowError('Access denied.')
        return identity[role]

    def _view(self, record, role, csrf):
        if role not in ('patient','delegate','staff'):
            raise FlowError('Access denied.')
        flow = decode(record['flow'])
        value = flow.view(self._actor(flow, role))
        for doc in value['documents']:
            imported = next((item for item in record['intake'] if item['document'] == doc['identifier']), None)
            doc['title'] = imported.get('title', 'Added document') if imported else TITLES.get(doc['identifier'], 'Added document')
            doc['upload_id'] = imported.get('upload_id') if imported else None
            doc['demo_pdf'] = not self.personal and doc['identifier'] in ('letter','rehab','appointment') and doc['text'] == {'letter':LETTER,'rehab':REHAB,'appointment':APPOINTMENT}[doc['identifier']]
            doc['origin'] = 'personal' if doc['identifier'] in flow.personal_documents else 'ward'
            delivered = flow.delivered.get(doc['identifier'])
            doc['delivered'] = bool(delivered and delivered.fingerprint == flow.documents[doc['identifier']].fingerprint)
        value.update(csrf=csrf, role=role, workspace_code=record.get('workspace_code'), person='Your documents' if self.personal else record.get('person', 'Giorgio Rinaldi'), owner=record['owner'],
            recipient='The ward you will contact' if self.personal else 'Continuity of care desk · Medicine', messages=record['messages'],
            acknowledgements=record['acknowledgements'], identity_mode='private_local' if self.personal else 'synthetic_demonstration',
            ai={'documentary': 'available' if self.ai else 'disabled',
                'explanation': 'patient_visible_ai' if self.ai else 'disabled',
                'question_check': 'available' if self.ai else 'disabled',
                'ward_assistance': 'available' if self.ai else 'disabled'})
        fingerprints = self._fingerprints(flow, role)
        value['ai_jobs'] = []
        for job in record.get('ai_jobs', []):
            if job['role'] != role:
                continue
            item = {k: job[k] for k in ('id', 'kind', 'status', 'stage', 'created_at_utc', 'started_at_utc', 'finished_at_utc', 'message', 'result', 'payload', 'asked_keys', 'retry_after_utc') if k in job}
            item['stale'] = job['fingerprints'] != fingerprints
            if job['kind'] == 'draft':
                q = flow.questions.get(job['payload']['question_id'])
                item['stale'] |= not q or q['generation'] != job['question_generation'] or q['status'] not in ('open', 'needs_review')
                item['stale'] |= job.get('delivery') != {k: d.fingerprint for k, d in flow.delivered.items()}
            if role != 'staff' and job['kind'] == 'explain' and item.get('result'):
                item['result'] = {k: v for k, v in item['result'].items() if k != 'explanation'}
            value['ai_jobs'].append(item)
        value['ai_pending'] = any(j['status'] in ('queued', 'running') for j in record.get('ai_jobs', []))
        value['ai_revision'] = record.get('ai_revision', 0)
        value['glossaries'] = {}
        value['glossary_enabled'] = self.precompute_glossary
        if self.precompute_glossary:
            with closing(sqlite3.connect(self.path, timeout=10)) as cache_db:
                for doc in value['documents']:
                    cached = cache_db.execute('SELECT status,updated_at_utc,record FROM glossary_cache WHERE cache_key=?',
                        (self._glossary_key(doc['text']),)).fetchone()
                    if cached:
                        value['glossaries'][doc['identifier']] = {'status': cached[0],
                            'updated_at_utc': cached[1], **json.loads(cached[2])}
        value['glossary_revision'] = hashlib.sha256(json.dumps(value['glossaries'], sort_keys=True).encode()).hexdigest()
        value['uploads'] = [{k: item[k] for k in ('id','title','pages','confirmed')}
            for item in record.get('uploads', []) if role == 'staff' or item['uploader_role'] != 'staff'
            or 'pdf-' + item['id'] in flow.delivered]
        for question in value['questions']:
            latest = flow.replies.get(question['answered_by'])
            last_response = latest.get('delivered_at_utc', '') if latest else ''
            reminders = [r for r in flow.reminders if question['id'] in r['questions']]
            question['active_reminders'] = [r for r in reminders if r['at_utc'] > last_response and question['status'] in ('open','needs_review')]
        if self.personal:
            value['ai'] = {'documentary': 'local_reading_only', 'explanation': 'local_reading_only', 'ward_assistance': 'not_connected'}
        if role == 'staff':
            value.update(tasks=record['tasks'], intake=record['intake'], bundle_confirmation=record['bundle_confirmation'])
        return value

    @staticmethod
    def _glossary_key(text):
        return hashlib.sha256(json.dumps([GLOSSARY_MODEL, 'medium', GLOSSARY_VERSION, text],
            ensure_ascii=False).encode()).hexdigest()

    def _glossary_inputs(self, record, role):
        flow = decode(record['flow'])
        candidates = {d['identifier']: d['text'] for d in flow.view(self._actor(flow, role))['documents']}
        for upload in record.get('uploads', []):
            if upload['confirmed'] or upload['uploader_role'] != role:
                continue
            if all(p['text'].strip() and (not self.page_transcriber or p.get('gemma')) for p in upload['pages']):
                candidates['pdf-' + upload['id']] = '\n\n'.join(
                    f"Page {p['number']}\n{p['text'].strip()}" for p in upload['pages'])
        return candidates

    def _schedule_glossaries(self, token, role):
        if not self.precompute_glossary:
            return
        with self.glossary_lock:
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                _, record = self._load(db, token)
                db.execute("DELETE FROM glossary_cache WHERE updated_at_utc<? AND status NOT IN ('queued','running')",
                    ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),))
                for text in self._glossary_inputs(record, role).values():
                    if not text.strip() or len(text) > 60000:
                        continue
                    key = self._glossary_key(text)
                    row = db.execute('SELECT status FROM glossary_cache WHERE cache_key=?', (key,)).fetchone()
                    if row and row[0] != 'interrupted':
                        continue
                    now = datetime.now(timezone.utc).isoformat()
                    db.execute('INSERT OR REPLACE INTO glossary_cache VALUES (?,?,?,?)',
                        (key, 'queued', now, json.dumps({'created_at_utc': now})))
                    self.glossary_queue.append((key, text, now))
            if self.glossary_queue and not self.glossary_running:
                self.glossary_running = True
                threading.Thread(target=self._run_glossaries, daemon=True).start()

    def _run_glossaries(self):
        while True:
            with self.glossary_lock:
                if not self.glossary_queue:
                    self.glossary_running = False
                    return
                key, text, created = self.glossary_queue.popleft()
            identifier = uuid.uuid4().hex
            started = None
            error_type = None
            validation_error = None
            try:
                with self.ai_slot, trace_task(identifier):
                    started = datetime.now(timezone.utc)
                    with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                        db.execute('UPDATE glossary_cache SET status=?,updated_at_utc=? WHERE cache_key=?',
                            ('running', started.isoformat(), key))
                    result = self.ai.glossary(text)
                status, output = 'completed', {'result': result, 'created_at_utc': created}
            except Exception as exc:
                error_type = type(exc).__name__
                validation_error = str(exc) if isinstance(exc, FlowError) else None
                status, output = 'failed', {'message': 'Medical wording could not be prepared. You can retry or ask the ward.',
                                           'created_at_utc': created}
            finished = datetime.now(timezone.utc)
            output.update(started_at_utc=started.isoformat() if started else None, finished_at_utc=finished.isoformat())
            try:
                with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                    db.execute('UPDATE glossary_cache SET status=?,updated_at_utc=?,record=? WHERE cache_key=?',
                        (status, finished.isoformat(), json.dumps(output), key))
                print(json.dumps({'event': 'ai_task', 'at_utc': finished.isoformat(), 'kind': 'clinical-glossary',
                    'task_id': identifier,
                    'status': status, 'error_type': error_type, 'validation_error': validation_error,
                    'queue_seconds': round((started-datetime.fromisoformat(created)).total_seconds(), 2) if started else None,
                    'processing_seconds': round((finished-started).total_seconds(), 2) if started else None}), flush=True)
            except sqlite3.Error:
                # A closed or removed local workspace must not leave a spinning worker.
                with self.glossary_lock:
                    self.glossary_running = False
                return

    @staticmethod
    def _fingerprints(flow, role):
        documents = dict(flow.documents) if role == 'staff' else dict(flow.delivered)
        if role != 'staff':
            documents.update({k: d for k, d in flow.documents.items() if k in flow.personal_documents})
        return {k: d.fingerprint for k, d in documents.items()}

    def _ai_documents(self, record, role, ids):
        view = self._view(record, role, '')
        available = {d['identifier']: d for d in view['documents']}
        if type(ids) is not list or not ids or any(type(k) is not str for k in ids) or len(set(ids)) != len(ids) or any(k not in available for k in ids):
            raise FlowError('Select documents available in this view.')
        rows = []
        for key in ids:
            doc = available[key]
            upload = next((u for u in record.get('uploads', []) if u['id'] == doc['upload_id']), None)
            document = Document(**{k: doc[k] for k in Document.__dataclass_fields__})
            rows.append({'identifier': key, 'title': doc['title'], 'fingerprint': document.fingerprint,
                'delivered': doc['delivered'], 'pages': [p['text'] for p in upload['pages']] if upload else [doc['text']]})
        if sum(len(d['pages']) for d in rows) > 5:
            raise FlowError('Select documents totalling at most five pages for one check.')
        if sum(len(p) for d in rows for p in d['pages']) > 60000:
            raise FlowError('Selected documents exceed the 60,000-character check limit.')
        return rows

    def start_ai(self, token, role, csrf, payload):
        if self.personal or not self.ai or role not in ('patient', 'staff'):
            raise FlowError('AI is unavailable in this workspace. Personal documents are never sent to AMD.')
        kind = payload.get('kind')
        if (kind not in ('analyze', 'explain', 'draft', 'answer')
                or kind == 'draft' and role != 'staff' or kind == 'answer' and role != 'patient'):
            raise FlowError('Choose an available AI action.')
        identifier = uuid.uuid4().hex
        with self.ai_admission:
            if len(self.ai_workers) >= 8:
                raise FlowError('The AI queue is full. Try again after an active check finishes.')
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                expected, record = self._load(db, token)
                if not isinstance(csrf, str) or not secrets.compare_digest(csrf, expected):
                    raise FlowError('Access denied.')
                flow = decode(record['flow'])
                actor = self._actor(flow, role)
                flow.check(actor, 'read', payload.get('revision'))
                jobs = record.setdefault('ai_jobs', [])
                if any(j['status'] in ('queued', 'running') for j in jobs):
                    raise FlowError('A check is already running for this case.')
                if len(jobs) >= 20:
                    raise FlowError('This session has reached twenty AI tasks. Open a new demo for further tests.')
                clean = {'documents': payload.get('documents')}
                documents = self._ai_documents(record, role, clean['documents'])
                question = None
                source = None
                if kind == 'answer':
                    questions = payload.get('questions')
                    if type(questions) is not list or not 1 <= len(questions) <= 10:
                        raise FlowError('Choose between one and ten questions to check.')
                    clean['questions'] = [{'id': f'q{i+1}', 'text': bounded_text(text, 2000)}
                                          for i, text in enumerate(questions)]
                if kind == 'draft':
                    question = flow.questions.get(payload.get('question_id'))
                    if not question or question['status'] not in ('open', 'needs_review'):
                        raise FlowError('Choose a question waiting for a reply.')
                    clean['question_id'] = question['id']
                if kind == 'explain':
                    if len(documents) != 1:
                        raise FlowError('Select one document for an explanation.')
                    source = next(d for d in flow.view(actor)['documents'] if d['identifier'] == documents[0]['identifier'])
                    clean['quote'] = bounded_text(payload.get('quote'), 5000)
                    if source['text'].count(clean['quote']) != 1:
                        raise FlowError('Select one exact, unique passage from the document.')
                    if 'question' in payload:
                        clean['question'] = bounded_text(payload['question'], 2000)
                job = {'id': identifier, 'kind': kind, 'role': role, 'status': 'queued', 'stage': 'Waiting for the AMD worker',
                    'created_at_utc': datetime.now(timezone.utc).isoformat(), 'payload': clean,
                    'fingerprints': self._fingerprints(flow, role), 'question_generation': question['generation'] if question else None,
                    'delivery': {k: d.fingerprint for k, d in flow.delivered.items()} if kind == 'draft' else None}
                jobs.append(job)
                flow.event(actor, 'ai_task_queued', task=identifier, kind=kind)
                record['flow'] = encode(flow)
                db.execute('UPDATE workspaces SET record=? WHERE token_hash=?', (json.dumps(record), self._workspace(db, token)))
            self.ai_workers.add(identifier)
            threading.Thread(target=self._run_ai, args=(token, identifier, documents, source, question), daemon=True).start()
        return self.view(token, role)

    def _run_ai(self, token, identifier, documents, source, question):
        def save(stage=None, result=None, error=None, retry_after_utc=None):
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                _, record = self._load(db, token)
                job = next(j for j in record['ai_jobs'] if j['id'] == identifier)
                if job['status'] in ('completed', 'failed'):
                    return job
                flow = decode(record['flow'])
                actor = self._actor(flow, job['role'])
                if job['fingerprints'] != self._fingerprints(flow, job['role']):
                    error, result = 'Documents changed during this check. Run it again on the current bundle.', None
                if job['kind'] == 'draft':
                    current = flow.questions.get(job['payload']['question_id'])
                    if not current or current['generation'] != job['question_generation'] or current['status'] not in ('open', 'needs_review'):
                        error, result = 'The conversation changed. Request a draft for the latest question.', None
                    if job['delivery'] != {k: d.fingerprint for k, d in flow.delivered.items()}:
                        error, result = 'Document delivery changed. Request a new draft with the current patient access.', None
                if error:
                    job.update(status='failed', message=error, stage='Check stopped', finished_at_utc=datetime.now(timezone.utc).isoformat())
                    if retry_after_utc:
                        job['retry_after_utc'] = retry_after_utc
                elif result is not None:
                    if job['kind'] == 'explain':
                        output = result['explanation']
                        fields = ('document_fingerprint','source_quote','plain_language','needs_clarification')
                        if 'question' in job['payload']:
                            fields += ('in_scope',)
                        exp_id = flow.submit_explanation(actor, source['identifier'], job['payload']['quote'],
                            {k: output[k] for k in fields},
                            {'model': result['calls'][0]['model'], 'run_id': identifier, 'mode': 'live'}, flow.revision,
                            question=job['payload'].get('question'))
                        result['explanation_id'] = exp_id
                    job.update(status='completed', result=result, stage='Result ready', finished_at_utc=datetime.now(timezone.utc).isoformat())
                else:
                    job.setdefault('started_at_utc', datetime.now(timezone.utc).isoformat())
                    job.update(status='running', stage=stage)
                record['ai_revision'] = record.get('ai_revision', 0) + 1
                record['flow'] = encode(flow)
                db.execute('UPDATE workspaces SET record=? WHERE token_hash=?', (json.dumps(record), self._workspace(db, token)))
            if result is not None or error:
                created = datetime.fromisoformat(job['created_at_utc'])
                started = datetime.fromisoformat(job.get('started_at_utc', job['finished_at_utc']))
                finished = datetime.fromisoformat(job['finished_at_utc'])
                print(json.dumps({'event': 'ai_task', 'at_utc': job['finished_at_utc'], 'task_id': identifier,
                    'kind': job['kind'], 'status': job['status'], 'queue_seconds': round((started-created).total_seconds(), 2),
                    'processing_seconds': round((finished-started).total_seconds(), 2)}), flush=True)
            if error:
                raise FlowError(error)
            return job
        try:
            with self.ai_slot, trace_task(identifier):
                job = save(stage='Starting the check')
                if job['kind'] == 'analyze':
                    result = self.ai.analyze(documents, lambda text: save(stage=text))
                elif job['kind'] == 'explain':
                    result = self.ai.explain(source, job['payload']['quote'], lambda text: save(stage=text),
                                             question=job['payload'].get('question'))
                elif job['kind'] == 'answer':
                    result = self.ai.answer(documents, job['payload']['questions'], lambda text: save(stage=text))
                else:
                    result = self.ai.draft(documents, question, lambda text: save(stage=text))
                save(result=result)
        except Exception as exc:
            from .documents import DocumentError
            message = str(exc) if isinstance(exc, (FlowError, DocumentError)) else 'This AI result could not be validated. No answer was published. Review the documents or retry.'
            try:
                save(error=message, retry_after_utc=getattr(exc, 'retry_after_utc', None))
            except (FlowError, StopIteration):
                pass
        finally:
            with self.ai_admission:
                self.ai_workers.discard(identifier)

    def view(self, token, role):
        self._schedule_glossaries(token, role)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            csrf, record = self._load(db, token)
            value = self._view(record, role, csrf)
            value['workspace_code'] = self._workspace(db, token)[:8].upper()
            return value

    def invite(self, token, csrf, role):
        if self.personal or role not in ('patient','staff'):
            raise FlowError('Sharing is available only in the demo.')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            expected, _ = self._load(db, token)
            if not isinstance(csrf, str) or not secrets.compare_digest(expected, csrf):
                raise FlowError('Access denied.')
            key = self._workspace(db, token)
            code = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
            db.execute('DELETE FROM workspace_invites WHERE workspace=?', (key,))
            db.execute('INSERT INTO workspace_invites VALUES (?,?,?,?)', (self.digest(code), key, expires, role))
            return {'code':code, 'role':role, 'expires_at_utc':expires}

    def join(self, code):
        if self.personal:
            raise FlowError('Personal workspaces cannot be shared.')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._purge_expired(db)
            row = db.execute('SELECT workspace,role FROM workspace_invites WHERE code_hash=?', (self.digest(code),)).fetchone()
            if not row:
                raise FlowError('Link used or expired. Create a new link from the original case.')
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO workspace_sessions VALUES (?,?)', (self.digest(token), row[0]))
            db.execute('DELETE FROM workspace_invites WHERE code_hash=?', (self.digest(code),))
            return token, row[1]

    def document_asset(self, token, role, identifier, page):
        from .demo_documents import demo_asset
        document = next((d for d in self.view(token, role)['documents'] if d['identifier'] == identifier), None)
        if not document or not document['demo_pdf'] or page not in (0, 1):
            raise FlowError('Document unavailable for this access.')
        return demo_asset(identifier, page)

    def validate_upload(self, token, role, csrf, revision):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            expected, record = self._load(db, token)
            if not isinstance(csrf, str) or not secrets.compare_digest(expected, csrf):
                raise FlowError('Refresh the page before continuing.')
            flow = decode(record['flow'])
            flow.check(self._actor(flow, role), 'upload', revision)
            if len(record.get('uploads', [])) >= 8:
                raise FlowError('You can add up to eight PDFs per session.')

    def upload(self, token, role, csrf, revision, data, title):
        from .local_pdf import extract_local_pdf
        self.validate_upload(token, role, csrf, revision)
        if self.allowed_pdf_digests is not None and hashlib.sha256(data).hexdigest() not in self.allowed_pdf_digests:
            raise FlowError('This public demo accepts the PDFs in the judge guide only. Download an example and upload it unchanged.')
        title = bounded_text(title, 180)
        result, images = extract_local_pdf(data, use_ocr=self.page_transcriber is None and self.local_ocr)
        if self.page_transcriber is None and not self.local_ocr:
            for page in result['pages']:
                page['method'] = 'pdf_text' if page['text'].strip() else 'manual_review'
                if not page['text'].strip():
                    page['warnings'].append('API OCR is disabled. Review the original and transcribe this page manually.')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            expected, record = self._load(db, token)
            if not isinstance(csrf, str) or not secrets.compare_digest(expected, csrf):
                raise FlowError('Refresh the page before continuing.')
            flow = decode(record['flow'])
            actor = self._actor(flow, role)
            flow.check(actor, 'upload', revision)
            if len(record.get('uploads', [])) >= 8:
                raise FlowError('You can add up to eight PDFs per session.')
            identifier = secrets.token_hex(16)
            record.setdefault('uploads', []).append({'id': identifier, 'title': title, 'confirmed': False,
                'uploader_role': role, **result})
            key = self._workspace(db, token)
            db.executemany('INSERT INTO upload_assets VALUES (?,?,?,?)',
                [(key, identifier, number, blob) for number, blob in enumerate([data, *images])])
            flow.event(actor, 'pdf_extracted_locally', upload=identifier, pages=len(images))
            record['flow'] = encode(flow)
            db.execute('UPDATE workspaces SET record=? WHERE token_hash=?', (json.dumps(record, ensure_ascii=False), key))
            return self._view(record, role, expected)

    def asset(self, token, role, upload_id, page):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            _, record = self._load(db, token)
            self._actor(decode(record['flow']), role)
            upload = next((u for u in record.get('uploads', []) if u['id'] == upload_id), None)
            if not upload or (role != 'staff' and upload['uploader_role'] == 'staff'
                    and 'pdf-' + upload_id not in record['flow']['delivered']):
                raise FlowError('Access denied.')
            row = db.execute('SELECT data FROM upload_assets WHERE workspace=? AND upload=? AND page=?',
                (self._workspace(db, token), upload_id, page)).fetchone()
            if not row:
                raise FlowError('Page unavailable.')
            return bytes(row[0])

    def read_page(self, token, role, csrf, revision, upload_id, page):
        if self.personal or not self.page_transcriber:
            raise FlowError('Gemma via API is available for invented examples. The shared AMD endpoint excludes personal health data.')
        if type(page) is not int or not 1 <= page <= 5:
            raise FlowError('Choose a valid page.')
        def checked(db):
            expected, record = self._load(db, token)
            if not isinstance(csrf, str) or not secrets.compare_digest(expected, csrf):
                raise FlowError('Access denied.')
            flow = decode(record['flow'])
            actor = self._actor(flow, role)
            flow.check(actor, 'upload', revision)
            upload = next((u for u in record.get('uploads', []) if u['id'] == upload_id), None)
            if not upload or upload['uploader_role'] != role or page > len(upload['pages']):
                raise FlowError('Page unavailable.')
            return expected, record, flow, actor, upload['pages'][page-1]
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            _, _, _, _, source = checked(db)
            if source.get('gemma'):
                return self.view(token, role)
            row = db.execute('SELECT data FROM upload_assets WHERE workspace=? AND upload=? AND page=?',
                             (self._workspace(db, token), upload_id, page)).fetchone()
            if not row:
                raise FlowError('Page unavailable.')
            image, reference = bytes(row[0]), source['original_text']
        with self.ai_slot:
            result = self.page_transcriber(image, reference)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            expected, record, flow, actor, source = checked(db)
            source['gemma'] = result
            if source['method'] == 'gemma_pending' and 'human_corrected' not in source:
                source.update(text=result['text'], method='gemma_api')
            flow.event(actor, 'page_transcribed_with_gemma_api', upload=upload_id, page=page)
            record['flow'] = encode(flow)
            db.execute('UPDATE workspaces SET record=? WHERE token_hash=?', (json.dumps(record, ensure_ascii=False), self._workspace(db, token)))
        return self.view(token, role)

    def delete(self, token, csrf):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            expected, _ = self._load(db, token)
            if not isinstance(csrf, str) or not secrets.compare_digest(expected, csrf):
                raise FlowError('Access denied.')
            db.execute('PRAGMA secure_delete=ON')
            key = self._workspace(db, token)
            db.execute('DELETE FROM upload_assets WHERE workspace=?', (key,))
            db.execute('DELETE FROM workspace_sessions WHERE workspace=?', (key,))
            db.execute('DELETE FROM workspace_invites WHERE workspace=?', (key,))
            db.execute('DELETE FROM workspaces WHERE token_hash=?', (key,))

    def mutate(self, token, role, csrf, action, payload):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            expected_csrf, record = self._load(db, token)
            if not isinstance(csrf, str) or not secrets.compare_digest(expected_csrf, csrf):
                raise FlowError('Refresh the page before continuing.')
            if role not in ('patient','delegate','staff') or type(payload) is not dict:
                raise FlowError('Access denied.')
            flow = decode(record['flow'])
            actor = self._actor(flow, role)
            revision = payload.get('revision')
            flow.check(actor, 'read', revision)
            if action == 'ask':
                questions = payload.get('questions')
                if type(questions) is not list or not 1 <= len(questions) <= 10:
                    raise FlowError('Choose between one and ten questions.')
                for text in questions:
                    flow.ask(actor, text, flow.revision)
            elif action == 'ask_findings':
                job = next((j for j in record.get('ai_jobs', []) if j['id'] == payload.get('job_id') and j['role'] == role and j['kind'] == 'analyze' and j['status'] == 'completed'), None)
                if not job or job['fingerprints'] != self._fingerprints(flow, role):
                    raise FlowError('Run the check again on the current documents.')
                keys = payload.get('keys')
                rows = {r['key']: r for r in job['result']['findings']}
                if type(keys) is not list or not 1 <= len(keys) <= 10 or any(type(k) is not str for k in keys) or len(set(keys)) != len(keys) or any(k not in rows for k in keys):
                    raise FlowError('Select one to ten clarification points.')
                if any(k in job.get('asked_keys', []) for k in keys):
                    raise FlowError('One of these points already has a conversation. Open My questions.')
                for key in keys:
                    row = rows[key]
                    qid = flow.ask(actor, row['question'], flow.revision)
                    flow.questions[qid]['source_finding'] = {'key': key, 'job_id': job['id'], 'citations': row['citations']}
                job.setdefault('asked_keys', []).extend(keys)
            elif action == 'approve_explanation':
                flow.approve_explanation(actor, payload.get('explanation_id'), revision)
            elif action == 'retry_glossary':
                if not self.precompute_glossary:
                    raise FlowError('AI explanations are unavailable.')
                documents = {d['identifier']: d for d in flow.view(actor)['documents']}
                doc = documents.get(payload.get('document_id'))
                if not doc:
                    raise FlowError('Document unavailable for this access.')
                db.execute("UPDATE glossary_cache SET status='interrupted' WHERE cache_key=? AND status='failed'",
                    (self._glossary_key(doc['text']),))
            elif action == 'ask_checked':
                if role != 'patient':
                    raise FlowError('Patient action required.')
                job = next((j for j in record.get('ai_jobs', []) if j['id'] == payload.get('job_id')
                    and j['role'] == role and j['kind'] == 'answer' and j['status'] == 'completed'), None)
                if not job or job['fingerprints'] != self._fingerprints(flow, role):
                    raise FlowError('Check the questions again against the current documents.')
                keys = payload.get('keys')
                rows = {r['question_id']: r for r in job['result']['answers']}
                if (type(keys) is not list or not 1 <= len(keys) <= 10 or any(type(k) is not str for k in keys)
                        or len(set(keys)) != len(keys) or any(k not in rows for k in keys)):
                    raise FlowError('Select the questions you want to send.')
                if any(k in job.get('asked_keys', []) for k in keys):
                    raise FlowError('One of these questions already has a conversation. Open My questions.')
                for key in keys:
                    flow.ask(actor, rows[key]['question'], flow.revision)
                job.setdefault('asked_keys', []).extend(keys)
            elif action == 'draft':
                flow.draft_reply(actor, payload.get('text'), payload.get('covered'), revision, reply_id=payload.get('reply_id'))
            elif action == 'reply_and_send':
                flow.authorize(actor, 'reply')
                rows = payload.get('replies')
                if (payload.get('recipient_confirmed') is not True or type(rows) is not list
                        or not 1 <= len(rows) <= 30 or any(type(row) is not dict for row in rows)):
                    raise FlowError('Choose the replies to send to this patient.')
                covered, reply_ids = set(), []
                for row in rows:
                    qid = row.get('question_id')
                    question = flow.questions.get(qid) if type(qid) is str else None
                    if not question or question['status'] not in ('open', 'needs_review') or qid in covered:
                        raise FlowError('Choose each question waiting for a reply once.')
                    previous = flow.replies.get(row.get('reply_id')) if type(row.get('reply_id')) is str else None
                    if row.get('reply_id') is not None and (not previous or previous['covered'] != [qid]):
                        raise FlowError('This draft belongs to a different conversation.')
                    covered.add(qid)
                    reply_ids.append(flow.draft_reply(actor, row.get('text'), [qid], flow.revision,
                                                     reply_id=row.get('reply_id')))
                flow.deliver_replies(actor, reply_ids, flow.revision)
            elif action == 'follow_up':
                flow.follow_up(actor, payload.get('question_id'), payload.get('reply_id'), payload.get('text'), revision)
            elif action == 'send_batch':
                if payload.get('recipient_confirmed') is not True:
                    raise FlowError('Confirm the recipient before delivery.')
                flow.deliver_replies(actor, payload.get('reply_ids'), revision)
            elif action == 'remind':
                if payload.get('confirmed') is not True:
                    raise FlowError('Confirm the reminder and recipient.')
                flow.remind(actor, payload.get('questions'), revision)
            elif action == 'message':
                flow.authorize(actor, 'reply')
                text = bounded_text(payload.get('text'), 2000)
                record['messages'].append({'text': text, 'author': actor.subject, 'at_utc': datetime.now(timezone.utc).isoformat()})
                flow.event(actor, 'ward_update_recorded')
            elif action == 'acknowledge':
                flow.acknowledge(actor, payload.get('question_id'), payload.get('reply_id'), revision)
            elif action == 'external_reply':
                if not self.personal or role != 'patient':
                    raise FlowError('This action is only available in the private workspace.')
                question = flow.questions.get(payload.get('question_id'))
                if not question or question['status'] not in ('open','needs_review'):
                    raise FlowError('Choose a question waiting for a reply.')
                text = bounded_text(payload.get('text'), 10000)
                source = bounded_text(payload.get('source'), 200)
                identifier = secrets.token_hex(16)
                flow.replies[identifier] = {'id': identifier, 'text': text, 'covered': [question['id']],
                    'author': source, 'origin': 'patient_recorded_external', 'recorded_by': actor.subject,
                    'approved': False, 'delivered': True, 'read': True, 'version': 1,
                    'delivered_at_utc': datetime.now(timezone.utc).isoformat()}
                question.update(status='answered', answered_by=identifier)
                flow.event(actor, 'external_reply_recorded', question=question['id'], reply=identifier)
            elif action == 'supplement':
                if self.personal:
                    raise FlowError('Demonstration documents are unavailable here.')
                if role not in ('patient','delegate'):
                    raise FlowError('Patient action required.')
                fixture = payload.get('fixture')
                if fixture not in ('letter','appointment','rehab'):
                    raise FlowError('Choose an available demonstration document.')
                text = {'letter': LETTER, 'appointment': APPOINTMENT, 'rehab': REHAB}[fixture]
                identifier = 'personal-' + fixture
                flow.add_document(actor, Document(identifier, flow.tenant, flow.patient_id, flow.episode_id, '1', text, False), revision)
                record['intake'].append({'document': identifier, 'origin': 'hand_delivered', 'content': text,
                    'confirmed_by': actor.subject, 'at_utc': datetime.now(timezone.utc).isoformat()})
            elif action == 'confirm_upload':
                flow.authorize(actor, 'upload')
                upload = next((u for u in record.get('uploads', []) if u['id'] == payload.get('upload_id')), None)
                if not upload or upload['confirmed'] or upload['uploader_role'] != role:
                    raise FlowError('Choose a document awaiting review.')
                if payload.get('reviewed') is not True:
                    raise FlowError('Compare every page with the original before accepting the text.')
                texts = payload.get('pages')
                if type(texts) is not list or len(texts) != len(upload['pages']):
                    raise FlowError('Review every page of the document.')
                texts = [bounded_text(text, 24000) for text in texts]
                content = '\n\n'.join(f'Page {number}\n{text}' for number, text in enumerate(texts, 1))
                bounded_text(content, 60000)
                identifier = 'pdf-' + upload['id']
                flow.add_document(actor, Document(identifier, flow.tenant, flow.patient_id, flow.episode_id,
                    '1', content, role == 'staff'), revision)
                for page, text in zip(upload['pages'], texts):
                    page['text'] = text
                    page['human_corrected'] = text != page['original_text']
                upload.update(confirmed=True, reviewed_by=actor.subject)
                record['intake'].append({'document': identifier, 'title': upload['title'], 'upload_id': upload['id'],
                    'origin': 'local_pdf', 'identity_verification': 'not_performed',
                    'confirmed_by': actor.subject, 'at_utc': datetime.now(timezone.utc).isoformat()})
            elif action == 'confirm_bundle':
                flow.authorize(actor, 'deliver_documents')
                ids = payload.get('documents')
                if type(ids) is not list or not ids or len(set(ids)) != len(ids) or any(k not in flow.documents or not flow.documents[k].approved for k in ids):
                    raise FlowError('Choose approved current documents.')
                if payload.get('reviewed') is not True:
                    raise FlowError('Review the selected bundle first.')
                flow.event(actor, 'bundle_confirmed', documents=ids)
                record['bundle_confirmation'] = {'revision': flow.revision, 'documents': ids, 'author': actor.subject,
                    'fingerprints': {k: flow.documents[k].fingerprint for k in ids}}
            elif action == 'deliver_documents':
                confirmation = record['bundle_confirmation']
                if not confirmation or confirmation['revision'] != revision:
                    raise FlowError('Confirm the current bundle before sharing.')
                flow.deliver_documents(actor, confirmation['documents'], revision)
                record['bundle_confirmation'] = None
            elif action == 'task':
                flow.authorize(actor, 'reply')
                if payload.get('kind') not in ('archive','clinical') or payload.get('question') not in flow.questions:
                    raise FlowError('Choose an existing point and an available task.')
                record['tasks'].append({'kind': payload['kind'], 'question': payload['question'], 'author': actor.subject,
                    'at_utc': datetime.now(timezone.utc).isoformat(), 'external_delivery': 'not_sent'})
                flow.event(actor, 'internal_task_recorded', question=payload['question'])
            elif action == 'delegate':
                if self.personal:
                    raise FlowError('Delegation is unavailable in this local workspace.')
                flow.set_delegate(actor, 'Sofia Rinaldi', payload.get('enabled'), revision,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=7))
            else:
                raise FlowError('This action is not available.')
            record['flow'] = encode(flow)
            db.execute('UPDATE workspaces SET record=? WHERE token_hash=?', (json.dumps(record, ensure_ascii=False), self._workspace(db, token)))
        return self.view(token, role)
