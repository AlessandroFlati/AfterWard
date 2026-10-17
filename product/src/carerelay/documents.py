"""Local extraction with explicit limits and source-review requirements."""

import base64
import hashlib
import json
from pathlib import Path
import re
import threading

import pypdfium2 as pdfium

from .evidence import ExtractionIssue, SourcePacket, SourcePage, digest
from .handoff import prepare_handoff

MAX_SOURCE_BYTES = 12 * 1024 * 1024
PDFIUM_LOCK = threading.RLock()
OCR_LOCK = threading.Lock()
_ocr_engine = None


class DocumentError(ValueError):
    pass


def fhir_packet(bundle: dict, *, patient_id: str, encounter_id: str, document_id: str, language: str) -> SourcePacket:
    """Import the final inline text subset; never fetch external FHIR URLs."""
    encoded = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise DocumentError("Source exceeds the 12 MiB limit.")
    prepare_handoff(bundle, patient_id=patient_id, encounter_id=encounter_id, document_id=document_id)
    document = next(entry["resource"] for entry in bundle["entry"]
                    if entry["resource"].get("resourceType") == "DocumentReference" and entry["resource"]["id"] == document_id)
    text = base64.b64decode(document["content"][0]["attachment"]["data"], validate=True).decode("utf-8")
    parts = text.split("\f")
    if len(parts) > 5:
        raise DocumentError("At most five source pages are supported.")
    pages = tuple(SourcePage(number=i, text=part, method="fhir_text") for i, part in enumerate(parts, 1))
    issues = [ExtractionIssue(code="empty_page", page=i, blocking=True)
              for i, part in enumerate(parts, 1) if not part.strip()]
    issues.extend(_identity_issues(pages, patient_id, encounter_id, required=False))
    issues.extend(_page_count_issues(pages))
    return SourcePacket(
        patient_id=patient_id, encounter_id=encounter_id, source_id=f"DocumentReference/{document_id}",
        source_revision=str((document.get("meta") or {}).get("versionId") or "unversioned"),
        source_sha256=digest(bundle), source_kind="fhir", language=language,
        pages=pages, issues=tuple(issues),
    )


def _read_pdf(data: bytes):
    """PDFium is not thread-safe, including across separate documents."""
    extracted = []
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise DocumentError("Cannot open this PDF; encrypted or malformed files are unsupported.") from exc
        try:
            if not 1 <= len(document) <= 5:
                raise DocumentError("A PDF must contain between one and five pages.")
            for number in range(len(document)):
                page = document[number]
                try:
                    width, height = page.get_size()
                    if not (36 <= width <= 1600 and 36 <= height <= 1600):
                        raise DocumentError("PDF page dimensions exceed the supported range.")
                    textpage = page.get_textpage()
                    try:
                        if textpage.count_chars() > 24000:
                            raise DocumentError("A source page exceeds the text limit.")
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                    large_image = False
                    objects = page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=3)
                    for index, obj in enumerate(objects):
                        if index >= 200:
                            raise DocumentError("PDF page contains too many image objects.")
                        left, bottom, right, top = obj.get_bounds()
                        if abs((right - left) * (top - bottom)) >= width * height * 0.12:
                            large_image = True
                    bitmap = page.render(scale=min(2.5, 2200 / max(width, height)))
                    try:
                        image = bitmap.to_pil().copy().convert("RGB")
                    finally:
                        bitmap.close()
                    extracted.append((text, image, large_image))
                finally:
                    page.close()
        finally:
            document.close()
    return extracted


def _ocr(image):
    global _ocr_engine
    with OCR_LOCK:
        if _ocr_engine is None:
            from rapidocr import RapidOCR, LangRec, ModelType, OCRVersion
            _ocr_engine = RapidOCR(params={
                "Rec.lang_type": LangRec.LATIN,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Global.log_level": "error",
                "Global.text_score": 0.0,
                "EngineConfig.onnxruntime.intra_op_num_threads": 4,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            })
        result = _ocr_engine(image)
        return "\n".join(result.txts or ()), tuple(float(value) for value in result.scores or ())


def _identity_issues(pages, patient_id, encounter_id, required=True):
    patterns = {
        "patient": (r"(?:Patient ID|ID paziente)\s*:\s*([A-Za-z0-9.-]+)", patient_id),
        "encounter": (r"(?:Encounter ID|ID episodio)\s*:\s*([A-Za-z0-9.-]+)", encounter_id),
    }
    found = {key: set() for key in patterns}
    for page in pages:
        for key, (pattern, expected) in patterns.items():
            values = re.findall(pattern, page.text, flags=re.IGNORECASE)
            found[key].update(values)
            if any(value != expected for value in values):
                return [ExtractionIssue(code="identity_mismatch", page=page.number, blocking=True)]
    if required and any(not values for values in found.values()):
        return [ExtractionIssue(code="identity_missing", blocking=True)]
    return []


def _page_count_issues(pages):
    declarations = []
    for page in pages:
        declarations.extend(re.findall(r"(?:Page|Pagina)\s+(\d+)\s+(?:of|di)\s+(\d+)", page.text, re.IGNORECASE))
    if declarations and (any(int(total) != len(pages) for _, total in declarations)
                         or [int(index) for index, _ in declarations] != list(range(1, len(pages) + 1))):
        return [ExtractionIssue(code="page_missing", blocking=True)]
    return []


def pdf_packet(data: bytes, *, patient_id: str, encounter_id: str, source_id: str,
               source_revision: str, language: str, image_dir: Path) -> SourcePacket:
    """Extract a bounded PDF; source identities require explicit supported headers.

    This prototype recognizes Patient ID / Encounter ID or their Italian labels.
    Page images stay available even for native text. A readable text layer does
    not prove that reading order, tables or the source itself are correct.
    """
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise DocumentError("A PDF byte stream is required.")
    if len(data) > MAX_SOURCE_BYTES:
        raise DocumentError("Source exceeds the 12 MiB limit.")
    source_hash = hashlib.sha256(data).hexdigest()
    extracted = _read_pdf(data)
    image_dir = Path(image_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    pages, issues = [], []
    for number, (text, image, large_image) in enumerate(extracted, 1):
        image_name = f"{source_hash[:24]}-{number}.png"
        image.save(image_dir / image_name)
        method, scores = "pdf_text", ()
        if large_image or len(text.strip()) < 40:
            try:
                text, scores = _ocr(image)
            except Exception as exc:
                raise DocumentError("OCR is unavailable; extraction was not completed.") from exc
            method = "ocr"
            issues.append(ExtractionIssue(code="ocr_review", page=number, blocking=False))
            if scores and min(scores) < 0.75:
                issues.append(ExtractionIssue(code="low_confidence", page=number, blocking=True))
        if len(text.strip()) < 25 or not any(char.isalpha() for char in text):
            issues.append(ExtractionIssue(code="unreadable", page=number, blocking=True))
        if len(text) > 24000:
            raise DocumentError("An extracted page exceeds the text limit.")
        issues.append(ExtractionIssue(code="layout_review", page=number, blocking=False))
        pages.append(SourcePage(number=number, text=text, method=method, image_name=image_name,
                                mean_confidence=sum(scores) / len(scores) if scores else None,
                                minimum_confidence=min(scores) if scores else None))
    issues.extend(_identity_issues(pages, patient_id, encounter_id))
    issues.extend(_page_count_issues(pages))
    methods = {page.method for page in pages}
    kind = "scanned_pdf" if methods == {"ocr"} else "printed_pdf" if methods == {"pdf_text"} else "mixed_pdf"
    return SourcePacket(patient_id=patient_id, encounter_id=encounter_id, source_id=source_id,
                        source_revision=source_revision, source_sha256=source_hash, source_kind=kind,
                        language=language, pages=tuple(pages), issues=tuple(issues))
