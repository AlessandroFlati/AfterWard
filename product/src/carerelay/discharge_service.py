"""Durable demonstration workspaces and isolated local personal PDF workspaces.

The fixed identity adapter is deliberately limited to synthetic demonstration
accounts. Personal mode has only the local owner, no staff or delegate identities.
Neither mode is a hospital SSO implementation; neither accepts client actors.
"""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3

from .discharge_flow import Actor, Document, DischargeFlow, FlowError, bounded_text

TITLES = {'letter': 'Discharge letter', 'rehab': 'Rehabilitation programme',
          'appointment': 'Follow-up appointment reminder', 'personal-sheet': 'Hand-delivered sheet'}
LETTER = ('Continue outpatient follow-up.\n'
          'Follow-up means later appointments at the outpatient clinic.\n\n'
          'Follow the rehabilitation programme on the sheet provided.\n'
          'Refer to the reminder for your follow-up appointment.\n')
REHAB = 'Exercises start after the rehabilitation medicine appointment on 2 December. The specialist will confirm when to begin.'
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
    def __init__(self, path, *, personal=False, page_transcriber=None, allowed_pdf_digests=None, local_ocr=True):
        if personal and page_transcriber:
            raise ValueError('The shared AMD API cannot process personal documents.')
        self.path = Path(path)
        self.personal = personal
        self.page_transcriber = page_transcriber
        self.allowed_pdf_digests = allowed_pdf_digests
        self.local_ocr = local_ocr
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS workspaces (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS upload_assets (workspace TEXT NOT NULL, upload TEXT NOT NULL, page INTEGER NOT NULL, data BLOB NOT NULL, PRIMARY KEY(workspace,upload,page))')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_sessions (token_hash TEXT PRIMARY KEY, workspace TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workspace_invites (code_hash TEXT PRIMARY KEY, workspace TEXT NOT NULL, expires TEXT NOT NULL, role TEXT NOT NULL)')
            self._purge_expired(db)

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
        if not self.personal:
            for key, text in (('letter', LETTER), ('rehab', REHAB), ('appointment', APPOINTMENT)):
                flow.add_document(identity['staff'], Document(key, flow.tenant, flow.patient_id, flow.episode_id, '1', text, True), flow.revision)
        if route == 'hospital':
            flow.deliver_documents(identity['staff'], ['letter'], flow.revision)
        if not self.personal:
            flow.set_delegate(identity['patient'], 'Sofia Rinaldi', True, flow.revision,
                expires_at=datetime.now(timezone.utc) + timedelta(days=7))
        record = {'schema': 1, 'flow': encode(flow), 'messages': [], 'acknowledgements': [],
            'bundle_confirmation': None, 'intake': [], 'owner': None if self.personal else 'Marta Ricci', 'tasks': []}
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
        value.update(csrf=csrf, role=role, workspace_code=record.get('workspace_code'), person='Your documents' if self.personal else 'Giorgio Rinaldi', owner=record['owner'],
            recipient='The ward you will contact' if self.personal else 'Continuity of care desk · Medicine', messages=record['messages'],
            acknowledgements=record['acknowledgements'], identity_mode='private_local' if self.personal else 'synthetic_demonstration',
            ai={'documentary': 'qualification_in_progress', 'explanation': 'qualification_in_progress', 'ward_assistance': 'adapter_pending'})
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

    def view(self, token, role):
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
            return self._view(record, role, expected)

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
            elif action == 'draft':
                flow.draft_reply(actor, payload.get('text'), payload.get('covered'), revision, reply_id=payload.get('reply_id'))
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
            return self._view(record, role, expected_csrf)
