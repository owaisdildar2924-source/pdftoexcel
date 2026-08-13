"""OCR path for scanned PDFs.

pypdfium2 (BSD/Apache) renders pages to images; pytesseract (Apache)
reads them. No AGPL anywhere.

Rules from the brief:
- blank pages have NO confidence, which is not the same as low
  confidence; they must not poison the document figure
- word boxes feed the same geometric engine as native text
- an incomplete read must never be presented as a complete one
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .core import Word

RENDER_SCALE = 300 / 72.0     # 300 dpi


def find_tesseract() -> str | None:
    """An app launched from the desktop gets a minimal PATH, so check the
    real install locations too, and verify the binary actually RUNS —
    existing on disk is not the same as working."""
    candidates = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates.append(os.path.join(bundled, "tesseract", "tesseract.exe"))
        candidates.append(os.path.join(bundled, "tesseract", "tesseract"))
    # onedir bundles: tesseract sits next to the executable
    exe_dir = os.path.dirname(getattr(sys, "executable", "") or "")
    if exe_dir:
        candidates.append(os.path.join(exe_dir, "tesseract", "tesseract.exe"))
        candidates.append(os.path.join(exe_dir, "_internal", "tesseract",
                                       "tesseract.exe"))
    on_path = shutil.which("tesseract")
    if on_path:
        candidates.append(on_path)
    candidates += [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract", "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            try:
                r = subprocess.run([c, "--version"], capture_output=True,
                                   timeout=20)
                if r.returncode == 0:
                    return c
            except Exception:
                continue
    return None


def extract_ocr(path: str) -> tuple[list[list[Word]], str, str]:
    """Returns (pages of words, full text, mean confidence string)."""
    import pypdfium2 as pdfium
    import pytesseract
    from pytesseract import Output

    tcmd = find_tesseract()
    if tcmd is None:
        raise RuntimeError(
            "no working OCR engine was found. The application bundle should "
            "include one; please report this.")
    pytesseract.pytesseract.tesseract_cmd = tcmd

    doc = pdfium.PdfDocument(path)
    pages_words: list[list[Word]] = []
    texts: list[str] = []
    page_confs: list[float] = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            bmp = page.render(scale=RENDER_SCALE)
            pil = bmp.to_pil()
            data = pytesseract.image_to_data(pil, output_type=Output.DICT,
                                             config="--psm 6")
            words: list[Word] = []
            confs: list[float] = []
            for j in range(len(data["text"])):
                txt = (data["text"][j] or "").strip()
                conf = float(data["conf"][j])
                if not txt or conf < 0:
                    continue
                # back to PDF coordinate scale (pt)
                x0 = data["left"][j] / RENDER_SCALE
                y0 = data["top"][j] / RENDER_SCALE
                w = data["width"][j] / RENDER_SCALE
                h = data["height"][j] / RENDER_SCALE
                words.append(Word(txt, x0, x0 + w, y0, y0 + h, conf))
                confs.append(conf)
            pages_words.append(words)
            lines: dict[int, list] = {}
            for wd in words:
                lines.setdefault(int(wd.top // 8), []).append(wd)
            texts.append("\n".join(
                " ".join(w.text for w in sorted(ws, key=lambda w: w.x0))
                for _, ws in sorted(lines.items())))
            # a page with no words has NO confidence — do not count it
            if confs:
                page_confs.append(sum(confs) / len(confs))
    finally:
        doc.close()
    mean = f"{sum(page_confs)/len(page_confs):.0f}%" if page_confs else "n/a"
    return pages_words, "\n".join(texts), mean
