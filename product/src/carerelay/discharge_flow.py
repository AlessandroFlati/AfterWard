"""Discharge workflow core, independent of inference and identity-provider integration.

Actor objects must come from a trusted server-side identity adapter. This module
enforces authorization but neither authenticates people nor sends notifications.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
import uuid


class FlowError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    subject: str
    tenant: str
    role: str
    patient_id: str | None = None
    episodes: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Document:
    identifier: str
    tenant: str
    patient_id: str
    episode_id: str
    revision: str
    text: str
    approved: bool

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def bounded_text(value, limit):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise FlowError("Text is empty or exceeds the supported length.")
    return value.strip()


def validate_explanation(document, selected_quote, output, *, question=None):
    """Check provenance and lexical invariants, not clinical semantic equivalence."""
    required = {"document_fingerprint", "source_quote", "plain_language", "needs_clarification"}
    if question is not None:
        bounded_text(question, 2000)
        required.add("in_scope")
    if type(output) is not dict or set(output) != required:
        raise FlowError("Unexpected explanation schema.")
    if output["document_fingerprint"] != document.fingerprint:
        raise FlowError("The explanation belongs to a different document revision.")
    quote = bounded_text(output["source_quote"], 5000)
    if quote != bounded_text(selected_quote, 5000) or document.text.count(quote) != 1:
        raise FlowError("The quoted passage is absent or ambiguous.")
    if type(output["needs_clarification"]) is not bool:
        raise FlowError("Clarification must be a boolean.")
    numbers = lambda text: re.findall(r"\d+(?:[.,:/-]\d+)*", text)
    if question is not None:
        if type(output["in_scope"]) is not bool or type(output["plain_language"]) is not str or len(output["plain_language"]) > 5000:
            raise FlowError("Invalid document-question response.")
        withheld = output["needs_clarification"] or not output["in_scope"]
        plain = "" if withheld else bounded_text(output["plain_language"], 5000)
        if set(numbers(plain)) - set(numbers(document.text)):
            raise FlowError("The explanation introduced a number absent from the document.")
        words = lambda text: re.findall(r"\w+", text.casefold())
        if not withheld and words(plain) == words(quote):
            raise FlowError("The explanation repeated the passage without answering. Try a more specific question.")
        return {**deepcopy(output), "source_quote": quote, "plain_language": plain,
                "needs_clarification": withheld, "semantic_validation": "not_clinically_validated"}
    plain = bounded_text(output["plain_language"], 5000)
    if numbers(quote) != numbers(plain):
        raise FlowError("Quantities or dates were added, removed or reordered.")
    negation = lambda text: re.findall(r"\b(?:not|never|without|no|non|mai|senza)\b|n't\b", text.casefold())
    if negation(quote) != negation(plain):
        raise FlowError("Negation markers changed; manual rewriting is required.")
    return {**deepcopy(output), "source_quote": quote, "plain_language": plain,
            "semantic_validation": "not_clinically_validated"}


class DischargeFlow:
    """One episode with revision-bound review and separately delivered documents."""

    def __init__(self, *, tenant, patient_id, episode_id, entry_route, reminder_interval=timedelta(hours=24)):
        if entry_route not in {"hospital", "patient"}:
            raise FlowError("Unknown entry route.")
        if not isinstance(reminder_interval, timedelta) or reminder_interval <= timedelta(0):
            raise FlowError("The reminder interval must be positive.")
        self.tenant, self.patient_id, self.episode_id = tenant, patient_id, episode_id
        self.entry_route = entry_route
        self.revision = 0
        self.documents = {}
        self.document_history = {}
        self.personal_documents = set()
        self.delivered = {}
        self.questions = {}
        self.reminders = []
        self.reminder_interval = reminder_interval
        self.replies = {}
        self.reply_history = {}
        self.explanations = {}
        self.delegates = {}
        self.request_closed = False
        self.events = []
        self.lock = threading.RLock()

    def authorize(self, actor, permission):
        if not isinstance(actor, Actor) or actor.tenant != self.tenant or not actor.subject:
            raise FlowError("Access denied.")
        if actor.role == "staff":
            allowed = self.episode_id in actor.episodes and permission in actor.permissions
        elif actor.role == "patient":
            allowed = actor.patient_id == self.patient_id and permission in {"read", "ask", "upload", "delegate", "remind"}
        elif actor.role == "delegate":
            expiry = self.delegates.get(actor.subject)
            allowed = expiry is not None and expiry > datetime.now(timezone.utc) and permission in {"read", "ask", "upload", "remind"}
        else:
            allowed = False
        if not allowed:
            raise FlowError("Access denied.")

    def check(self, actor, permission, revision):
        self.authorize(actor, permission)
        if type(revision) is not int or revision != self.revision:
            raise FlowError("The episode changed; refresh before continuing.")

    def event(self, actor, action, **details):
        self.revision += 1
        self.events.append({"at_utc": datetime.now(timezone.utc).isoformat(), "actor": actor.subject,
                            "action": action, "revision": self.revision, **details})

    def set_delegate(self, actor, subject, enabled, revision, *, expires_at=None):
        with self.lock:
            self.check(actor, "delegate", revision)
            if type(enabled) is not bool:
                raise FlowError("Delegation requires an explicit choice.")
            subject = bounded_text(subject, 200)
            if enabled:
                if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
                    raise FlowError("A delegation requires a future timezone-aware expiry.")
                self.delegates[subject] = expires_at
            else:
                self.delegates.pop(subject, None)
            self.event(actor, "delegation_changed", delegate=subject, enabled=enabled)

    def add_document(self, actor, document, revision):
        with self.lock:
            self.check(actor, "upload", revision)
            if not isinstance(document, Document) or (document.tenant, document.patient_id, document.episode_id) != (self.tenant, self.patient_id, self.episode_id):
                raise FlowError("The document belongs to another boundary.")
            if not document.identifier or not document.revision or type(document.approved) is not bool:
                raise FlowError("Invalid document metadata.")
            bounded_text(document.text, 60000)
            if actor.role != "staff" and document.approved:
                raise FlowError("A patient upload cannot claim hospital approval.")
            existing = self.documents.get(document.identifier)
            if actor.role != "staff" and existing and document.identifier not in self.personal_documents:
                raise FlowError("A personal upload cannot replace a hospital record.")
            if existing and existing.fingerprint != document.fingerprint and existing.revision == document.revision:
                raise FlowError("Changed content requires a new source revision.")
            if existing and existing.fingerprint == document.fingerprint:
                return
            self.documents[document.identifier] = document
            self.document_history.setdefault(document.identifier, []).append(document)
            if actor.role == "staff":
                self.personal_documents.discard(document.identifier)
            else:
                self.personal_documents.add(document.identifier)
            for question in self.questions.values():
                if question["status"] == "answered":
                    question["status"] = "needs_review"
            self.request_closed = bool(self.questions) and all(q["status"] == "resolved" for q in self.questions.values())
            for reply in self.replies.values():
                if not reply["delivered"]:
                    reply["approved"] = False
            for explanation in self.explanations.values():
                if explanation["document_id"] == document.identifier:
                    explanation["approved"] = False
            self.event(actor, "document_updated", document=document.identifier)

    def deliver_documents(self, actor, identifiers, revision):
        with self.lock:
            self.check(actor, "deliver_documents", revision)
            if type(identifiers) is not list or not identifiers or any(type(k) is not str for k in identifiers) or len(set(identifiers)) != len(identifiers):
                raise FlowError("Choose documents to deliver.")
            records = [self.documents.get(key) for key in identifiers]
            if any(record is None or not record.approved for record in records):
                raise FlowError("Only approved current documents can be delivered.")
            for record in records:
                self.delivered[record.identifier] = record
            self.event(actor, "documents_delivered", documents=identifiers)

    def ask(self, actor, text, revision):
        with self.lock:
            self.check(actor, "ask", revision)
            text = bounded_text(text, 2000)
            if len(self.questions) >= 30:
                raise FlowError("Too many questions in one request.")
            identifier = uuid.uuid4().hex
            self.questions[identifier] = {"id": identifier, "text": text, "author": actor.subject,
                "status": "open", "answered_by": None, "generation": 1,
                "messages": [{"id": uuid.uuid4().hex, "text": text, "author": actor.subject,
                    "at_utc": datetime.now(timezone.utc).isoformat(), "reply_to": None}],
                "resolved_at_utc": None}
            self.request_closed = False
            for reply in self.replies.values():
                if not reply["delivered"]:
                    reply["approved"] = False
            self.event(actor, "question_added", question=identifier)
            return identifier

    def draft_reply(self, actor, text, covered, revision, *, reply_id=None):
        with self.lock:
            self.check(actor, "reply", revision)
            text = bounded_text(text, 10000)
            if type(covered) is not list or not covered or any(type(k) is not str for k in covered) or len(set(covered)) != len(covered) or any(key not in self.questions for key in covered):
                raise FlowError("Choose the questions this reply addresses.")
            if reply_id is not None:
                old = self.replies.get(reply_id)
                if old is None or old["delivered"] or old["author"] != actor.subject:
                    raise FlowError("This reply cannot be edited by this actor.")
            identifier = reply_id or uuid.uuid4().hex
            if reply_id:
                self.reply_history.setdefault(reply_id, []).append(deepcopy(self.replies[reply_id]))
            self.replies[identifier] = {"id": identifier, "text": text, "covered": list(covered),
                "author": actor.subject, "origin": "professional_direct", "approved": False,
                "recipient_patient_id": self.patient_id, "delivered": False, "read": False,
                "question_generations": {key: self.questions[key].get("generation", 1) for key in covered},
                "document_revision": self.document_revision(),
                "version": self.replies.get(identifier, {}).get("version", 0) + 1}
            self.event(actor, "reply_drafted", reply=identifier)
            return identifier

    def remind(self, actor, question_ids, revision):
        """Record one manual reminder in the existing thread; no external delivery."""
        with self.lock:
            self.check(actor, "remind", revision)
            if actor.role not in {"patient", "delegate"}:
                raise FlowError("A reminder is a patient or delegated action.")
            if self.request_closed:
                raise FlowError("The request is closed.")
            if (type(question_ids) is not list or not question_ids
                    or any(type(k) is not str for k in question_ids)
                    or len(set(question_ids)) != len(question_ids)
                    or any(k not in self.questions or self.questions[k]["status"] not in {"open", "needs_review"} for k in question_ids)):
                raise FlowError("Choose unresolved questions in this request.")
            now = datetime.now(timezone.utc)
            if any(any(key in reminder["questions"] for key in question_ids)
                    and now < datetime.fromisoformat(reminder["at_utc"]) + self.reminder_interval
                    for reminder in self.reminders):
                raise FlowError("A reminder was already recorded; wait until the displayed next time.")
            identifier = uuid.uuid4().hex
            self.reminders.append({"id": identifier, "at_utc": now.isoformat(), "author": actor.subject,
                "questions": list(question_ids), "status": "recorded_in_thread", "external_delivery": "not_sent"})
            self.event(actor, "patient_reminder_recorded", reminder=identifier, questions=list(question_ids))
            return identifier

    def document_revision(self):
        return {key: doc.fingerprint for key, doc in self.documents.items()}

    def follow_up(self, actor, question_id, reply_id, text, revision):
        with self.lock:
            self.check(actor, "ask", revision)
            if actor.role not in {"patient", "delegate"}:
                raise FlowError("Patient action required.")
            question = self.questions.get(question_id)
            reply = self.replies.get(reply_id)
            if not question or not reply or not reply["delivered"] or question["answered_by"] != reply_id:
                raise FlowError("Open the latest reply before asking for clarification.")
            if question["status"] not in {"answered", "resolved"}:
                raise FlowError("A clarification is already pending in this thread.")
            text = bounded_text(text, 2000)
            if len(question["messages"]) >= 50:
                raise FlowError("Thread message limit reached.")
            question["messages"].append({"id": uuid.uuid4().hex, "text": text, "author": actor.subject,
                "reply_to": reply_id, "at_utc": datetime.now(timezone.utc).isoformat()})
            question.update(status="open", generation=question["generation"] + 1, resolved_at_utc=None)
            self.request_closed = False
            self.event(actor, "clarification_requested", question=question_id, reply=reply_id)

    def deliver_replies(self, actor, reply_ids, revision):
        """Validate the entire selection before publishing and signing any reply."""
        with self.lock:
            self.check(actor, "reply", revision)
            if (type(reply_ids) is not list or not reply_ids or len(reply_ids) > 30
                    or any(type(key) is not str for key in reply_ids) or len(set(reply_ids)) != len(reply_ids)):
                raise FlowError("Choose the drafts to send.")
            selected, covered = [], set()
            for identifier in reply_ids:
                reply = self.replies.get(identifier)
                if not reply or reply["delivered"] or reply["author"] != actor.subject:
                    raise FlowError("Choose your current unsent drafts.")
                if reply.get("document_revision") != self.document_revision():
                    raise FlowError("Documents changed. Review and save the draft again.")
                for key in reply["covered"]:
                    question = self.questions[key]
                    if (key in covered or question["status"] not in {"open", "needs_review"}
                            or reply.get("question_generations", {}).get(key) != question.get("generation", 1)):
                        raise FlowError("A question changed or has overlapping drafts. Review the selection.")
                    covered.add(key)
                selected.append(reply)
            for reply in selected:
                reply.update(delivered=True, approved=True, delivered_at_utc=datetime.now(timezone.utc).isoformat())
                for key in reply["covered"]:
                    self.questions[key].update(status="answered", answered_by=reply["id"], resolved_at_utc=None)
                self.event(actor, "reply_available_in_portal", reply=reply["id"], questions=reply["covered"])

    def acknowledge(self, actor, question_id, reply_id, revision):
        with self.lock:
            self.check(actor, "ask", revision)
            if actor.role not in {"patient", "delegate"}:
                raise FlowError("Patient action required.")
            question = self.questions.get(question_id)
            if not question or question["status"] != "answered" or question["answered_by"] != reply_id:
                raise FlowError("Only the latest unanswered acknowledgement can be recorded.")
            question.update(status="resolved", resolved_at_utc=datetime.now(timezone.utc).isoformat(), resolved_by=actor.subject)
            self.replies[reply_id]["read"] = True
            self.request_closed = all(q["status"] == "resolved" for q in self.questions.values())
            self.event(actor, "question_resolved_by_patient", question=question_id, reply=reply_id)

    def submit_explanation(self, actor, document_id, selected_quote, output, provenance, revision, *, question=None):
        with self.lock:
            self.check(actor, "explain" if actor.role == "staff" else "read", revision)
            visible = self.documents if actor.role == "staff" else {
                **self.delivered, **{k: d for k, d in self.documents.items() if k in self.personal_documents}}
            document = visible.get(document_id)
            if document is None:
                raise FlowError("A source visible to this person is required.")
            value = validate_explanation(document, selected_quote, output, question=question)
            if type(provenance) is not dict or set(provenance) != {"model", "run_id", "mode"} or provenance["mode"] not in {"recorded", "live", "offline_contract_test"}:
                raise FlowError("Explicit execution provenance is required.")
            if not all(type(v) is str and 0 < len(v) <= 200 for v in provenance.values()):
                raise FlowError("Invalid execution provenance.")
            identifier = uuid.uuid4().hex
            self.explanations[identifier] = {**value, "id": identifier, "document_id": document_id,
                "provenance": deepcopy(provenance), "approved": False, "reviewer": None, "requested_by": actor.subject}
            if question is not None:
                self.explanations[identifier]["question"] = bounded_text(question, 2000)
            self.event(actor, "explanation_drafted", explanation=identifier)
            return identifier

    def approve_explanation(self, actor, identifier, revision):
        with self.lock:
            self.check(actor, "explain", revision)
            explanation = self.explanations.get(identifier)
            if not explanation or explanation["needs_clarification"]:
                raise FlowError("This passage requires clarification before publication.")
            if self.documents[explanation["document_id"]].fingerprint != explanation["document_fingerprint"]:
                raise FlowError("The source changed.")
            explanation.update(approved=True, reviewer=actor.subject)
            self.event(actor, "explanation_reviewed", explanation=identifier)

    def view(self, actor):
        with self.lock:
            self.authorize(actor, "read")
            staff = actor.role == "staff"
            visible = dict(self.documents) if staff else dict(self.delivered)
            if not staff:
                visible.update({k: v for k, v in self.documents.items() if k in self.personal_documents})
            explanations = [deepcopy(e) for e in self.explanations.values() if
                e["document_id"] in visible and visible[e["document_id"]].fingerprint == e["document_fingerprint"]]
            if not staff:
                for explanation in explanations:
                    if explanation["needs_clarification"]:
                        explanation["plain_language"] = ""
            pending = [key for key, q in self.questions.items() if q["status"] in {"open", "needs_review"}]
            now = datetime.now(timezone.utc)
            reminder_options = []
            for key in pending:
                previous = [r for r in self.reminders if key in r["questions"]]
                next_time = datetime.fromisoformat(previous[-1]["at_utc"]) + self.reminder_interval if previous else None
                reminder_options.append({"question_id": key, "available": not staff and (not next_time or now >= next_time),
                    "next_allowed_at_utc": next_time.isoformat() if next_time else None})
            eligible = [item["question_id"] for item in reminder_options if item["available"]]
            next_reminder = min((item["next_allowed_at_utc"] for item in reminder_options if item["next_allowed_at_utc"]), default=None)
            reminder_reason = ("patient_action_only" if staff else "request_closed" if self.request_closed
                else "no_pending_questions" if not pending else "cooldown" if not eligible else "available")
            return {"episode_id": self.episode_id, "entry_route": self.entry_route, "revision": self.revision,
                "documents": [deepcopy(d.__dict__) for d in visible.values()], "questions": deepcopy(list(self.questions.values())),
                "replies": [deepcopy(r) for r in self.replies.values() if staff or r["delivered"]],
                "reminders": deepcopy(self.reminders), "reminder_action": {"available": reminder_reason == "available",
                    "reason": reminder_reason, "pending_question_ids": pending,
                    "next_allowed_at_utc": next_reminder, "eligible_question_ids": eligible, "options": reminder_options},
                "explanations": explanations, "closed": self.request_closed, "external_messages_sent": False,
                "events": deepcopy(self.events) if staff else []}
