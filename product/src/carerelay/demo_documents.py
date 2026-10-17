"""Packaged PDF originals for the three existing demonstration documents."""
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from .documents import _read_pdf


@lru_cache(maxsize=6)
def demo_asset(identifier, page):
    if identifier not in ('letter','rehab','appointment') or page not in (0, 1):
        raise ValueError('Unknown demonstration document.')
    data = (Path(__file__).parent/'assets/demo'/f'{identifier}.pdf').read_bytes()
    if page == 0:
        return data
    image = _read_pdf(data)[0][1]
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()
