"""PDF text extraction: native text layer, with scan detection.

No AGPL dependencies: pdfplumber (MIT) for native text, pypdfium2
(BSD/Apache) + pytesseract (Apache) for the OCR path in ocr.py.
"""
from __future__ import annotations

import logging
import warnings

from .core import Word

log = logging.getLogger(__name__)
warnings.filterwarnings("ignore")
logging.getLogger("pdfminer").setLevel(logging.ERROR)


def extract_native(path: str) -> tuple[list[list[Word]], str, bool, list[str]]:
    """Returns (pages of words, full text, is_native, per-page text).

    is_native is False when the document has too little text layer to be a
    born-digital PDF (then the OCR path should be used).

    The per-page text list is pdfplumber's own extract_text() per page,
    kept verbatim (not the geometric engine's reconstruction from words) so
    that anything built from page slices of a bundled multi-statement PDF
    matches exactly what a single-statement file would have produced.
    """
    import pdfplumber

    pages: list[list[Word]] = []
    texts: list[str] = []
    total_chars = 0
    with pdfplumber.open(path) as pdf:
        npages = len(pdf.pages)
        for p in pdf.pages:
            ws = []
            for w in p.extract_words(x_tolerance=1.8, y_tolerance=2.5,
                                     keep_blank_chars=False):
                ws.append(Word(w["text"], float(w["x0"]), float(w["x1"]),
                               float(w["top"]), float(w["bottom"])))
            pages.append(ws)
            t = p.extract_text() or ""
            texts.append(t)
            total_chars += len(t)
    is_native = total_chars >= 200 * max(1, npages // 3)
    return pages, "\n".join(texts), is_native, texts
