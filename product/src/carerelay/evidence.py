"""Bounded evidence and question contracts shared by every input adapter."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExtractionIssue(Contract):
    code: Literal["unreadable", "low_confidence", "identity_mismatch", "identity_missing", "page_missing", "layout_review", "ocr_review", "empty_page"]
    page: int | None = Field(default=None, ge=1, le=5)
    blocking: bool


class SourcePage(Contract):
    number: int = Field(ge=1, le=5)
    text: str = Field(max_length=24000)
    method: Literal["fhir_text", "pdf_text", "ocr", "staff_text"]
    image_name: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]+\.png$")
    mean_confidence: float | None = Field(default=None, ge=0, le=1)
    minimum_confidence: float | None = Field(default=None, ge=0, le=1)


class SourcePacket(Contract):
    schema_version: Literal[1] = 1
    patient_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9.-]+$")
    encounter_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9.-]+$")
    source_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9./-]+$")
    source_revision: str = Field(min_length=1, max_length=40)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_kind: Literal["fhir", "printed_pdf", "scanned_pdf", "mixed_pdf", "staff_reply"]
    language: Literal["it", "en"]
    pages: tuple[SourcePage, ...] = Field(min_length=1, max_length=5)
    issues: tuple[ExtractionIssue, ...] = ()

    @model_validator(mode="after")
    def ordered_pages(self):
        if [page.number for page in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("Pages must be unique, contiguous and ordered.")
        if sum(len(page.text) for page in self.pages) > 60000:
            raise ValueError("Extracted packet exceeds the 60,000 character limit.")
        if any(issue.page is not None and issue.page > len(self.pages) for issue in self.issues):
            raise ValueError("An extraction issue references a missing page.")
        return self

    @property
    def blocked(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class Citation(Contract):
    page: int = Field(ge=1, le=5)
    start: int = Field(ge=0, le=24000)
    end: int = Field(ge=1, le=24000)
    quote: str = Field(min_length=1, max_length=2500)

    @model_validator(mode="after")
    def span_length(self):
        if self.end <= self.start or self.end - self.start != len(self.quote):
            raise ValueError("Citation offsets must exactly span its quote.")
        return self


GapCode = Literal["missing_attachment", "missing_amount", "missing_frequency", "missing_duration", "missing_start", "missing_contact", "missing_appointment", "conflict"]
Topic = Literal["cream", "medicine", "rehabilitation", "followup", "document"]


class Finding(Contract):
    code: GapCode
    topic: Topic
    citations: tuple[Citation, ...] = Field(min_length=1, max_length=4)

    @property
    def key(self) -> str:
        return f"{self.topic}:{self.code}"


class Draft(Contract):
    packet_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    findings: tuple[Finding, ...] = Field(max_length=20)
    method: Literal["amd", "local_explicit_gap_baseline", "staff_authored"]
    model: str | None = Field(default=None, max_length=200)
    run_id: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def model_provenance(self):
        if self.method == "amd" and not self.model:
            raise ValueError("AMD drafts require the configured model identifier.")
        if self.method != "amd" and self.model is not None:
            raise ValueError("A local or staff draft cannot claim a model execution.")
        return self


def validate_draft(packet: SourcePacket, draft: Draft) -> Draft:
    """Check provenance, not semantic truth: a staff review is still required."""
    if packet.blocked:
        raise ValueError("Source extraction or identity is blocked.")
    if draft.packet_fingerprint != packet.fingerprint:
        raise ValueError("The draft belongs to a different source revision.")
    keys = [finding.key for finding in draft.findings]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate finding keys are not accepted.")
    for finding in draft.findings:
        if len({(c.page, c.start, c.end) for c in finding.citations}) != len(finding.citations):
            raise ValueError("A finding cannot repeat the same citation.")
        for citation in finding.citations:
            if citation.page > len(packet.pages):
                raise ValueError("Citation references a missing page.")
            text = packet.pages[citation.page - 1].text
            if text[citation.start:citation.end] != citation.quote:
                raise ValueError("Citation does not exactly match the source text.")
        if finding.code == "conflict" and len(finding.citations) < 2:
            raise ValueError("A conflict must cite both statements.")
    return draft


def cite(packet: SourcePacket, page: int, quote: str) -> Citation:
    text = packet.pages[page - 1].text
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0:
        raise ValueError("Quote must occur exactly once on the selected page.")
    return Citation(page=page, start=start, end=start + len(quote), quote=quote)


QUESTIONS = {
    "missing_attachment": ("Potete inviare l'allegato richiamato nel documento?", "Could you send the attachment referred to in the document?"),
    "missing_amount": ("Quale quantità è prevista per questa persona?", "What amount is intended for this person?"),
    "missing_frequency": ("Con quale frequenza va seguita questa indicazione?", "How often should this instruction be followed?"),
    "missing_duration": ("Per quanto tempo va seguita questa indicazione?", "For how long should this instruction be followed?"),
    "missing_start": ("Quando è previsto l'inizio di questa attività?", "When is this activity intended to start?"),
    "missing_contact": ("A chi possiamo rivolgerci per chiarire questa indicazione?", "Who can we contact to clarify this instruction?"),
    "missing_appointment": ("Potete confermare come e quando organizzare il controllo?", "Could you confirm how and when to arrange the follow-up?"),
    "conflict": ("Queste indicazioni sembrano diverse: quale documento deve essere chiarito o aggiornato?", "These instructions appear different: which document needs clarification or updating?"),
}


def question_text(finding: Finding, language: str = "it") -> str:
    return QUESTIONS[finding.code][0 if language == "it" else 1]
