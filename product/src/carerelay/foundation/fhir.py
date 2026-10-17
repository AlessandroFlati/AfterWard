"""Selected TrustedRisk extractors. CareRelay scopes input before calling them."""

from __future__ import annotations
from .schemas import ClinicalNoteSection


def _entries(bundle: dict | None) -> list[dict]:
    if not isinstance(bundle, dict):
        return []
    return bundle.get("entry", []) or []


def extract_medications(bundle: dict | None) -> tuple[list[str], list[str]]:
    cited: list[str] = []
    bullets: list[str] = []
    for entry in _entries(bundle):
        resource = entry.get("resource", {})
        if resource.get("resourceType") != "MedicationRequest":
            continue
        rid = resource.get("id", "MedicationRequest")
        med_block = (
            resource.get("medicationCodeableConcept")
            or resource.get("medicationReference")
            or {}
        )
        text = (
            med_block.get("text")
            or (med_block.get("coding") or [{}])[0].get("display")
            or "unknown medication"
        )
        cited.append(rid)
        bullets.append(f"  - {text} (cite {rid})")
    return bullets, cited


def extract_allergies(bundle: dict | None) -> tuple[list[str], list[str]]:
    cited: list[str] = []
    bullets: list[str] = []
    for entry in _entries(bundle):
        resource = entry.get("resource", {})
        if resource.get("resourceType") != "AllergyIntolerance":
            continue
        rid = resource.get("id", "AllergyIntolerance")
        text = (
            (resource.get("code") or {}).get("text")
            or ((resource.get("code") or {}).get("coding") or [{}])[0].get("display")
            or "unspecified allergy"
        )
        cited.append(rid)
        bullets.append(f"  - {text} (cite {rid})")
    return bullets, cited


def render_bullets_section(
    section_id: str,
    title: str,
    bullets: list[str],
    cited: list[str],
    empty_text: str,
) -> ClinicalNoteSection:
    body = "\n".join(bullets) if bullets else empty_text
    return ClinicalNoteSection(
        section_id=section_id, title=title, body=body,
        cited_evidence_ids=cited,
    )


_SOAP_SECTION_HEADERS: dict[str, tuple[str, ...]] = {
    "subjective":  ("SUBJECTIVE", "S:"),
    "objective":   ("OBJECTIVE", "O:"),
    "assessment":  ("ASSESSMENT AND PLAN", "ASSESSMENT", "A&P", "A AND P",
                     "IMPRESSION", "A:"),
    "plan":        ("PLAN", "P:", "RECOMMENDATIONS"),
    "hpi":         ("HISTORY OF PRESENT ILLNESS", "HPI", "PRESENT ILLNESS"),
    "pmh":         ("PAST MEDICAL HISTORY", "PMH", "MEDICAL HISTORY"),
    "psh":         ("PAST SURGICAL HISTORY", "PSH", "SURGICAL HISTORY"),
    "fh":          ("FAMILY HISTORY", "FH"),
    "sh":          ("SOCIAL HISTORY", "SH"),
    "ros":         ("REVIEW OF SYSTEMS", "ROS"),
    "pe":          ("PHYSICAL EXAMINATION", "PHYSICAL EXAM", "PE",
                     "EXAMINATION"),
    "labs":        ("LABS", "LABORATORY", "LAB DATA",
                     "LABORATORY DATA"),
    "imaging":     ("IMAGING", "RADIOLOGY"),
    "consultation_reason": (
        "REASON FOR CONSULTATION", "CONSULTATION REASON",
        "REASON FOR CONSULT", "REQUEST",
    ),
    "hospital_course":     ("HOSPITAL COURSE", "COURSE OF HOSPITALIZATION",
                                "COURSE IN HOSPITAL"),
    "discharge_disposition": ("DISCHARGE DISPOSITION", "DISPOSITION"),
    "patient_instructions":  ("PATIENT INSTRUCTIONS", "INSTRUCTIONS",
                                  "DISCHARGE INSTRUCTIONS"),
    "followup_plan":        ("FOLLOW-UP PLAN", "FOLLOWUP PLAN",
                                "FOLLOW UP", "FOLLOWUP",
                                "FOLLOW-UP", "FOLLOW-UP APPOINTMENTS"),
}


def _decode_attachment_body(content: dict) -> str:
    """Decode an attachment.data b64 to plaintext. Returns "" on any failure."""
    import base64
    att = content.get("attachment") or {}
    data = att.get("data")
    if not isinstance(data, str):
        return ""
    try:
        return base64.b64decode(data).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _iter_clinical_note_bodies(bundle: dict | None):
    """Yield each (DocumentReference, plaintext_body) from the bundle.

    Includes any DocumentReference whose attachment decodes; relies on
    the scribe pipeline downstream to score relevance via section
    coverage rather than filtering by category here.
    """
    if not isinstance(bundle, dict):
        return
    for entry in bundle.get("entry") or []:
        r = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(r, dict) or r.get("resourceType") != "DocumentReference":
            continue
        for content in r.get("content") or []:
            body = _decode_attachment_body(content) if isinstance(content, dict) else ""
            if body:
                yield r, body


def extract_soap_section(
    bundle: dict | None, section_name: str,
) -> str:
    """Pull the named SOAP section from the most relevant DocumentReference.

    Args:
        bundle: FHIR R4 Bundle.
        section_name: one of the keys of `_SOAP_SECTION_HEADERS`. Case
            insensitive.

    Returns:
        The plaintext body of the matched section, stripped of leading /
        trailing whitespace. Empty string when the bundle contains no
        DocumentReference whose plaintext has the requested header.

    The matcher walks every DocumentReference body, finds the first
    instance of any header alias in `_SOAP_SECTION_HEADERS[section_name]`,
    and returns the substring up to the next recognised SOAP header (or
    end of body). No fabrication: when no header matches, the empty
    string surfaces and the scribe tool's existing abstain path runs.
    """
    import re
    section_key = section_name.lower()
    aliases = _SOAP_SECTION_HEADERS.get(section_key)
    if not aliases:
        return ""
    # Build a regex that matches any alias as a header line. Header may
    # appear at start of body OR after a blank line; it ends with
    # optional ":" and whitespace before the actual content.
    alias_pattern = "|".join(
        re.escape(a) for a in aliases
    )
    # Header form: alias must be followed by a colon (with optional
    # whitespace) or by a newline; this avoids matching "PE" as the
    # start of "Penicillin" or "FH" inside "of his". Two-letter aliases
    # are particularly risky without this anchor.
    header_re = re.compile(
        rf"(?:^|\n)[ \t]*({alias_pattern})[ \t]*(?::[ \t]*|\n)",
        re.IGNORECASE,
    )
    # Set of all SOAP headers -- used to detect where the matched
    # section ends. Same anchor logic as the start regex.
    all_aliases: list[str] = []
    for aliases_set in _SOAP_SECTION_HEADERS.values():
        all_aliases.extend(aliases_set)
    stop_re = re.compile(
        r"\n[ \t]*(" + "|".join(re.escape(a) for a in all_aliases)
        + r")[ \t]*(?::[ \t]*|\n)",
        re.IGNORECASE,
    )

    best: tuple[int, str] = (-1, "")
    for r, body in _iter_clinical_note_bodies(bundle):
        m = header_re.search(body)
        if not m:
            continue
        tail = body[m.end():]
        stop_m = stop_re.search(tail)
        if stop_m:
            tail = tail[: stop_m.start()]
        tail = tail.strip()
        if not tail:
            continue
        # Score: prefer DocumentReference whose category is clinical-note
        # (PO uploads them as such); fall back to first match.
        score = 0
        for cat in r.get("category") or []:
            for c in (cat or {}).get("coding") or []:
                if (c.get("code") or "").lower() in (
                    "clinical-note", "progress-note", "consultation-note",
                    "discharge-summary", "history-and-physical",
                ):
                    score += 5
        if score > best[0]:
            best = (score, tail)
    return best[1]
