"""Prepare a cited draft from an explicitly selected encounter and final source note."""

import base64
from copy import deepcopy
import hashlib
import json

from .foundation.fhir import extract_soap_section, render_bullets_section
from .foundation.merkle_audit import compute_merkle_audit_root

SECTIONS = {
    "hospital_course": "Hospital course recorded by staff",
    "patient_instructions": "Instructions recorded by staff",
    "followup_plan": "Follow-up recorded by staff",
}


def _digest(payload):
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _reference(value):
    return value.get("reference") if isinstance(value, dict) else None


def prepare_handoff(bundle, *, patient_id, encounter_id, document_id):
    """Return a draft only; this function cannot approve or release a handover.

    Supports a bounded FHIR R4 subset with local Type/id references and one
    final text/plain document. No live FHIR requests or model calls occur.
    Missing information remains missing. Source text is preserved verbatim.
    """
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        raise ValueError("A FHIR Bundle is required.")
    entries = bundle.get("entry")
    if not isinstance(entries, list) or not entries:
        raise ValueError("The bundle must contain resources.")
    for identifier in (patient_id, encounter_id, document_id):
        if not isinstance(identifier, str) or not identifier or "/" in identifier:
            raise ValueError("Use non-empty local resource IDs, without a Type/ prefix.")
    resources = {}
    for entry in entries:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            raise ValueError("Each entry must contain a resource object.")
        kind, identifier = resource.get("resourceType"), resource.get("id")
        if not isinstance(kind, str) or not isinstance(identifier, str) or not identifier:
            raise ValueError("Each resource needs resourceType and a stable id.")
        key = f"{kind}/{identifier}"
        if key in resources:
            raise ValueError(f"Duplicate resource reference: {key}")
        resources[key] = resource

    patient_ref = f"Patient/{patient_id}"
    encounter_ref = f"Encounter/{encounter_id}"
    document_ref = f"DocumentReference/{document_id}"
    if patient_ref not in resources:
        raise ValueError("The selected patient is missing.")
    encounter = resources.get(encounter_ref)
    if not encounter or _reference(encounter.get("subject")) != patient_ref:
        raise ValueError("The selected encounter must belong to the selected patient.")
    document = resources.get(document_ref)
    if not document or _reference(document.get("subject")) != patient_ref:
        raise ValueError("The selected document must belong to the selected patient.")
    refs = (document.get("context") or {}).get("encounter") or []
    if encounter_ref not in [_reference(item) for item in refs]:
        raise ValueError("The source document must explicitly reference the selected encounter.")
    if document.get("status") != "current" or document.get("docStatus") != "final":
        raise ValueError("Select a current, final source document; draft/superseded notes are rejected.")
    contents = document.get("content")
    if not isinstance(contents, list) or len(contents) != 1:
        raise ValueError("This foundation accepts one explicit text/plain attachment per document.")
    attachment = contents[0].get("attachment") if isinstance(contents[0], dict) else None
    if not isinstance(attachment, dict) or attachment.get("contentType") != "text/plain":
        raise ValueError("A text/plain attachment is required; PDF/OCR ingestion is pending.")
    try:
        encoded = attachment["data"]
        if not isinstance(encoded, str) or len(encoded) > 180_000:
            raise ValueError("Invalid attachment size.")
        text = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("The source must be valid base64 containing UTF-8 text.") from exc
    if not text.strip():
        raise ValueError("The source document is empty.")

    # The upstream parser sees only the user-selected document, avoiding its
    # original best-match heuristic across unrelated or older notes.
    scoped = {"resourceType": "Bundle", "entry": [{"resource": deepcopy(document)}]}
    sections = []
    gaps = []
    for key, title in SECTIONS.items():
        content = extract_soap_section(scoped, key)
        if not content:
            gaps.append({"section": key, "reason": "not_found_in_selected_source", "status": "unresolved"})
            continue
        section = render_bullets_section(key, title, [content], [document_ref], "")
        sections.append(section.model_dump())
    payload = {
        "project": "CareRelay", "schema_version": 1, "status": "draft",
        "patient_reference": patient_ref, "encounter_reference": encounter_ref,
        "source_reference": document_ref,
        "source_version": (document.get("meta") or {}).get("versionId"),
        "source_sha256": _digest(document),
        "sections": sections, "gaps": gaps,
        "requires_staff_review": True, "release_ready": False,
        "model_execution": None,
        "limitations": [
            "Source excerpts only; no clinical correctness or completeness assessment.",
            "No cross-document conflict detection, medication reconciliation or translation yet.",
            "Approval, caregiver confirmation and persistent follow-up are not implemented.",
        ],
    }
    event = {"event_id": "draft-" + _digest(payload), "event_type": "draft_prepared", "payload": payload}
    return {"handoff": payload, "audit_event": event,
            "audit_root": compute_merkle_audit_root([event]).model_dump()}
