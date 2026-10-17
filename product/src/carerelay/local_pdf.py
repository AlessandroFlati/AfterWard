"""Local PDF intake without synthetic identity headers or inference calls."""
from io import BytesIO
import hashlib
import threading

from .documents import DocumentError, MAX_SOURCE_BYTES, _read_pdf, _ocr

INTAKE_LOCK = threading.Lock()


def extract_local_pdf(data, *, use_ocr=True):
    if not isinstance(data, bytes) or not data.startswith(b'%PDF-') or len(data) > MAX_SOURCE_BYTES:
        raise DocumentError('Choose a valid PDF, up to 12 MiB.')
    if not INTAKE_LOCK.acquire(blocking=False):
        raise DocumentError('A document is being read. Wait and try again.')
    try:
        try:
            extracted = _read_pdf(data)
        except DocumentError as exc:
            raise DocumentError('Cannot read this PDF. Use 1 to 5 standard-size pages without password protection.') from exc
        pages, images = [], []
        for number, (native, image, large_image) in enumerate(extracted, 1):
            method, text, scores = 'pdf_text', native, ()
            if use_ocr and (large_image or len(native.strip()) < 40):
                try:
                    text, scores = _ocr(image)
                except Exception as exc:
                    raise DocumentError('Local OCR unavailable. No document was added.') from exc
                method = 'ocr'
            if len(text) > 24000:
                raise DocumentError('A page exceeds the text limit.')
            warnings = []
            if not use_ocr:
                method = 'gemma_pending'
            if not text.strip() and use_ocr:
                warnings.append('No text recognized: transcribe this page or leave the document unconfirmed.')
            if scores and min(scores) < .75:
                warnings.append('Some words are uncertain: compare the text carefully with the page.')
            if large_image and native.strip():
                warnings.append('Mixed page: check printed text and reading order too.')
            pages.append({'number': number, 'text': text, 'original_text': text, 'method': method,
                'minimum_confidence': min(scores) if scores else None, 'warnings': warnings})
            buffer = BytesIO()
            image.save(buffer, format='PNG')
            images.append(buffer.getvalue())
        return {'sha256': hashlib.sha256(data).hexdigest(), 'pages': pages}, images
    finally:
        INTAKE_LOCK.release()
