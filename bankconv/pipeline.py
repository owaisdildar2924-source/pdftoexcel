"""Folder pipeline and per-file conversion orchestration.

Folder layout inside the working folder chosen by the user:
    input/       user drops PDFs here
    output/      spreadsheets appear here, plus _logs/
    processed/   source PDFs move here after success
    failed/      source PDFs move here after failure

Rules enforced here (from the brief):
- never delete or overwrite a source document; move it
- version output filenames on collision rather than replacing
- write files atomically (write to temp, then rename)
- every file ends in exactly one of three states, and the user is told
  which: converted & proven, converted but unproven, or not converted
  with a plain-language reason.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from .pdftext import extract_native
from .parse import parse_statement, split_statements
from .profiles import detect_profile
from .reconcile import reconcile_block
from .settlement import (detect_settlement, parse_colonial, parse_jordan,
                         parse_shelton)
from .excel_out import write_bank_workbook, write_settlement_workbook

PRODUCER = "PDF to Excel 2.0.0"


@dataclass
class FileOutcome:
    source: str
    state: str            # proven | unproven | failed
    detail: str
    output: Optional[str] = None
    rows: int = 0
    flagged: int = 0


def _versioned(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} (v{n}){ext}"):
        n += 1
    return f"{base} (v{n}){ext}"


def _atomic_save(write_fn: Callable[[str], None], final_path: str) -> str:
    final_path = _versioned(final_path)
    d = os.path.dirname(final_path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=d)
    os.close(fd)
    try:
        write_fn(tmp)
        os.replace(tmp, final_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return final_path


ACCOUNT_RE = re.compile(
    r"Account\s*(?:number|#)?[:\s]+([\dXx*\- ]{6,20})", re.I)


def _find_account(text: str) -> Optional[str]:
    m = ACCOUNT_RE.search(text)
    if m:
        acct = m.group(1).strip()
        if sum(c.isdigit() for c in acct) >= 4:
            return acct
    return None


def convert_file(pdf_path: str, out_dir: str,
                 use_ocr_if_needed: bool = True) -> FileOutcome:
    name = os.path.basename(pdf_path)
    out_name = os.path.splitext(name)[0] + ".xlsx"
    out_path = os.path.join(out_dir, out_name)

    try:
        pages, text, is_native, page_texts = extract_native(pdf_path)
    except Exception as e:
        return FileOutcome(name, "failed",
                           f"The PDF could not be opened or read: {e}")

    method = "Native text layer (exact)"
    ocr_conf = "n/a"
    if not is_native:
        if not use_ocr_if_needed:
            return FileOutcome(name, "failed",
                               "This PDF has no text layer (it is a scan) "
                               "and OCR is disabled.")
        try:
            from .ocr import extract_ocr
            pages, text, ocr_conf = extract_ocr(pdf_path)
            page_texts = None   # OCR reconstructs text itself; no verbatim
                                # per-page original to fall back on
            method = "OCR (scanned pages)"
        except Exception as e:
            return FileOutcome(name, "failed",
                               f"This PDF is a scan and OCR failed: {e}")

    # ---- settlements ----------------------------------------------------
    kind = detect_settlement(text)
    if kind == "colonial_van_lines_settlement":
        res = parse_colonial(text)
    elif kind == "jordan_carriers_deduction_report":
        res = parse_jordan(text)
    elif kind == "shelton_trucking_settlement":
        res = parse_shelton(text)
    else:
        res = None
    if res is not None:
        flagged = sum(1 for x in res.rows if x.status != "OK")
        recon = ("PASSED" if res.recon_passed
                 else "FAILED" if res.recon_passed is False else "NOT PROVABLE")
        info = dict(source=name, format=f"{res.display} ({res.key})",
                    confidence="97%", method=method, ocr_conf=ocr_conf,
                    rows=len(res.rows), review=flagged, failed=0,
                    recon=recon, recon_detail=res.recon_detail,
                    producer=PRODUCER,
                    pages=f"{len(pages)} of {len(pages)} "
                          f"(settlement parser reads the whole document text, "
                          f"not page-by-page)")
        final = _atomic_save(
            lambda p: write_settlement_workbook(p, res.rows, info), out_path)
        state = "proven" if res.recon_passed else "unproven"
        return FileOutcome(name, state, res.recon_detail, final,
                           len(res.rows), flagged)

    # ---- bank statements ------------------------------------------------
    # A single PDF is not always a single statement: some issuers (Synchrony
    # private-label cards) bundle several months into one file, each with
    # its own opening/closing balance. split_statements() finds the
    # boundaries via repeated 'Page 1 of N' markers; an ordinary one-period
    # PDF yields exactly one chunk, so this is a no-op for every other
    # format.
    # page_texts is only None on the OCR path (no verbatim per-page
    # original to hand it); split_statements() falls back to its own
    # geometric text reconstruction in that case, so a scanned multi-month
    # bundle still gets split instead of being silently treated as one
    # statement with one opening balance for the whole file.
    chunks = split_statements(pages, page_texts)
    # a single chunk is the overwhelming common case (one PDF = one
    # statement): use the ORIGINAL whole-document text exactly as before,
    # not a re-join of page slices, so nothing about existing formats can
    # change even by a whitespace difference
    if len(chunks) == 1:
        chunks = [(pages, text)]
    total_pages = len(pages)
    pages_read = 0
    incomplete_reasons: list[str] = []
    profile = None
    conf = 0
    blocks = []
    for chunk_pages, chunk_text in chunks:
        prof, cf, chunk_blocks, sp = parse_statement(chunk_pages, chunk_text)
        if profile is None or len(chunk_blocks) and any(b.txns for b in chunk_blocks):
            profile, conf = prof, cf
        pages_read += min(sp.stopped_page or sp.total_pages, sp.total_pages)
        if sp.unread_tail_has_money():
            incomplete_reasons.append(
                f"Parsing stopped at page {sp.stopped_page} of this "
                f"statement, but later pages still contain dollar figures "
                f"that were never read.")
        # tag each block with its statement period so bundled months are
        # distinguishable in the output, without losing any of them
        period_label = ""
        if len(chunks) > 1:
            m = re.search(r"(billing cycle from [\d/]+ to [\d/]+)", chunk_text, re.I) \
                or re.search(r"(closing date:?\s*[A-Za-z]+ \d{1,2},? \d{4})", chunk_text, re.I) \
                or re.search(r"(statement (?:period|date)[:\s]+[^\n]{4,40})", chunk_text, re.I)
            period_label = (m.group(1).strip() if m else "").title()
        for b in chunk_blocks:
            if period_label:
                b.name = f"{b.name} {period_label}".strip() if b.name else period_label
            blocks.append(b)
    if profile is None:
        profile, conf = detect_profile(text[:6000])

    for b in blocks:
        reconcile_block(b)
    ntxn = sum(len(b.txns) for b in blocks)
    if ntxn == 0:
        return FileOutcome(
            name, "failed",
            "No transactions could be recognised in this document. It may "
            "be a format this tool has not seen before; the source PDF has "
            "been moved to the failed folder untouched.")
    flagged = sum(1 for b in blocks for t in b.txns if t.status != "OK")
    all_proven = all(b.recon_passed for b in blocks) and flagged == 0 \
        and not incomplete_reasons
    any_failed = any(b.recon_passed is False for b in blocks)
    recon = ("PASSED" if all_proven else
             "FAILED" if any_failed else "NOT PROVABLE")
    detail = " | ".join(
        (f"[{b.name or 'account'}] " if len(blocks) > 1 else "") +
        (b.recon_detail or "") for b in blocks)
    if incomplete_reasons:
        detail = "INCOMPLETE READ: " + " ".join(incomplete_reasons) + " | " + detail
    account = _find_account(text)
    pages_note = f"{total_pages} of {total_pages}" if not incomplete_reasons \
        else f"{pages_read} of {total_pages} (see reconciliation detail)"
    info = dict(source=name,
                format=f"{profile.display} ({profile.key})"
                       + (f", {len(chunks)} statements in this file" if len(chunks) > 1 else ""),
                confidence=f"{conf}%", method=method, ocr_conf=ocr_conf,
                rows=ntxn, review=flagged, failed=0,
                recon=recon, recon_detail=detail, producer=PRODUCER,
                pages=pages_note)
    final = _atomic_save(
        lambda p: write_bank_workbook(p, name, profile.institution or
                                      "Unknown", account, blocks, info),
        out_path)
    state = "proven" if all_proven else "unproven"
    return FileOutcome(name, state, detail, final, ntxn, flagged)


def process_folder(workdir: str,
                   progress: Optional[Callable[[str], None]] = None,
                   use_ocr: bool = True) -> list[FileOutcome]:
    inp = os.path.join(workdir, "input")
    out = os.path.join(workdir, "output")
    logs = os.path.join(out, "_logs")
    processed = os.path.join(workdir, "processed")
    failed = os.path.join(workdir, "failed")
    for d in (inp, out, logs, processed, failed):
        os.makedirs(d, exist_ok=True)

    outcomes: list[FileOutcome] = []
    pdfs = sorted(f for f in os.listdir(inp)
                  if f.lower().endswith(".pdf"))
    for i, f in enumerate(pdfs, 1):
        if progress:
            progress(f"Converting {i}/{len(pdfs)}: {f}")
        src = os.path.join(inp, f)
        try:
            oc = convert_file(src, out, use_ocr_if_needed=use_ocr)
        except Exception as e:      # never fail silently
            oc = FileOutcome(f, "failed",
                             f"Unexpected error while converting: {e}")
        outcomes.append(oc)
        dest_dir = processed if oc.state in ("proven", "unproven") else failed
        dest = _versioned(os.path.join(dest_dir, f))
        try:
            shutil.move(src, dest)
        except Exception:
            pass                    # leaving the source in input is safe
    _write_log(logs, outcomes)
    return outcomes


def _write_log(logs_dir: str, outcomes: list[FileOutcome]) -> None:
    import datetime
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H%M%S")
    path = os.path.join(logs_dir, f"run {stamp}.txt")
    lines = []
    for oc in outcomes:
        head = {"proven": "CONVERTED, ARITHMETIC PROVEN",
                "unproven": "CONVERTED BUT NOT PROVEN",
                "failed": "NOT CONVERTED"}[oc.state]
        lines.append(f"{oc.source}\n  {head}\n  {oc.detail}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) or "No PDF files found in the input folder.")
