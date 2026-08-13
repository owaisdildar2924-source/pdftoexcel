"""The geometric statement parser.

One engine for every issuer. Profiles supply vocabulary only.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from .core import (CENT, AccountBlock, DateToken, Line, Money, Txn, Word,
                   cluster_right_edges, group_lines, parse_date_token,
                   parse_money, parse_monthname_date, resolve_year)
from .profiles import CATCH_ALL, Profile, detect_profile

MIN_DESC_ALPHA = 4          # a real transaction always has a description
COLUMN_TOL = 7.0            # pt tolerance matching an amount to a column
HEADER_WORDS = {"credits", "debits", "balance", "amount", "deposits",
                "withdrawals", "additions", "subtractions", "charges"}

BALANCE_FWD_RE = re.compile(r"balance\s*forward", re.I)
# matched against text with ALL spaces removed: PDFs split words mid-token
# ('Ending B alance'), which made a real ending-balance row parse as a txn
BEGIN_ROW_SQ = re.compile(r"^(?:\d[\d/\-]*)?beginningbalance", re.I)
END_ROW_SQ = re.compile(r"^(?:\d[\d/\-]*)?endingbalance", re.I)
TOTALS_RE = re.compile(r"^\s*Totals?\b", re.I)
SECTION_TOTAL_RE = re.compile(
    r"^Total\s+(.{3,60}?)\s+\$?([\d,]+\.\d{2})-?\s*$", re.I)

MONTH_RE = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"

PERIOD_RES = [
    # January 01, 2022 through January 31, 2022  (also glued 'through')
    re.compile(MONTH_RE + r"\.?\s+(\d{1,2}),?\s*(\d{4})\s*(?:through|thru|to|[-–])\s*"
               + MONTH_RE + r"\.?\s+(\d{1,2}),?\s*(\d{4})", re.I),
    # 01/01/22 THRU 01/31/22
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s*(?:through|thru|to|[-–])\s*"
               r"(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I),
    # Nov 16 - Dec 15, 2024   (year printed once)
    re.compile(MONTH_RE + r"\.?\s+(\d{1,2})\s*[-–]\s*" + MONTH_RE
               + r"\.?\s+(\d{1,2}),?\s*(\d{4})", re.I),
    # 'Your previous balance as of 02/28/2025 ... new balance as of
    # 03/31/2025' — some statements state the period only this way, with
    # no 'through'/'to' range and too few other full dates in the document
    # for the last-resort span heuristic to find (this format prints every
    # transaction date without a year, e.g. '03/03', so there may be only
    # two full dates in the entire file). Bounded gap, not unbounded, so a
    # stray later repeat of 'balance as of' cannot pair with the wrong
    # date. \s* rather than a literal space between every word: pdfplumber
    # sometimes extracts this exact phrase with zero inter-word spacing
    # ('previousbalanceasof02/28/2025'), a font/kerning quirk seen on some
    # months' statements but not others from the very same issuer.
    re.compile(r"previous\s*balance\s*as\s*of\s*(\d{1,2})/(\d{1,2})/(\d{2,4})"
               r".{0,300}?new\s*balance\s*as\s*of\s*(\d{1,2})/(\d{1,2})/(\d{2,4})",
               re.I | re.S),
]
SINGLE_DATE_RE = re.compile(MONTH_RE + r"\.?\s+(\d{1,2}),\s*(\d{4})", re.I)

from .core import MONTHS


def _mk_date(mon: str, day: str, year: str) -> Optional[date]:
    m = MONTHS.get(mon.lower()[:3])
    try:
        return date(int(year), m, int(day)) if m else None
    except ValueError:
        return None


def find_period(text: str) -> Optional[tuple[date, date]]:
    for i, pat in enumerate(PERIOD_RES):
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        if i == 0:
            d1, d2 = _mk_date(g[0], g[1], g[2]), _mk_date(g[3], g[4], g[5])
        elif i == 1 or i == 3:
            y1 = int(g[2]) + (2000 if len(g[2]) == 2 else 0)
            y2 = int(g[5]) + (2000 if len(g[5]) == 2 else 0)
            try:
                d1, d2 = date(y1, int(g[0]), int(g[1])), date(y2, int(g[3]), int(g[4]))
            except ValueError:
                continue
        else:
            d2 = _mk_date(g[2], g[3], g[4])
            if d2 is None:
                continue
            m1 = MONTHS.get(g[0].lower()[:3])
            y1 = d2.year if m1 <= d2.month else d2.year - 1
            try:
                d1 = date(y1, m1, int(g[1]))
            except ValueError:
                continue
        if d1 and d2 and timedelta(0) <= (d2 - d1) <= timedelta(days=400):
            return (d1, d2)
    # fallback: a single "Month DD, YYYY" (statement date) = period end
    m = SINGLE_DATE_RE.search(text)
    if m:
        d2 = _mk_date(m.group(1), m.group(2), m.group(3))
        if d2:
            return (d2 - timedelta(days=35), d2)
    # last resort: span of fully-dated tokens found in the document.
    # Weighted toward dates rather than bare four-digit numbers (a stray
    # figure in a rate table must not outvote real dates).
    full = re.findall(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2})\b", text)
    dates = []
    for mo, dy, yr in full:
        y = int(yr) + (2000 if len(yr) == 2 else 0)
        try:
            dates.append(date(y, int(mo), int(dy)))
        except ValueError:
            pass
    if len(dates) >= 3:
        lo, hi = min(dates), max(dates)
        if (hi - lo) <= timedelta(days=400):
            return (lo, hi)
    return None


# ------------------------------------------------------------------ rows ----

class RowScan:
    """Result of scanning one visual line."""
    def __init__(self, line: Line):
        self.line = line
        self.words = line.sorted_words()
        self.date: Optional[DateToken] = None
        self.date2: Optional[DateToken] = None
        self.moneys: list[tuple[Money, Word]] = []
        self.desc_words: list[Word] = []
        self._scan()

    def _scan(self) -> None:
        ws = self.words
        i = 0
        lead_word: Optional[Word] = None
        # leading date token(s)
        if ws:
            d = parse_date_token(ws[0].text)
            if d is None and len(ws) >= 2:
                d = parse_monthname_date(ws[0].text, ws[1].text)
                if d:
                    i = 2
            elif d is not None:
                i = 1
            if d is None and len(ws) >= 2:
                # Some issuers print a reference number before the date
                # ('F638800EF000FR063 03/03 03/03 CREDIT ONE REWARD ...').
                # A leading token that isn't itself a date must not sink
                # the whole row when the very next token clearly is one —
                # that silently dropped every transaction in the
                # 'Payments, Credits, and Adjustments' section. Keep the
                # leading token as description text instead of losing it.
                d_after_ref = parse_date_token(ws[1].text)
                if d_after_ref is not None:
                    d = d_after_ref
                    lead_word = ws[0]
                    i = 2
            self.date = d
        if lead_word is not None:
            self.desc_words.append(lead_word)
        if self.date and i < len(ws):
            d2 = parse_date_token(ws[i].text)
            if d2 is None and i + 1 < len(ws):
                d2 = parse_monthname_date(ws[i].text, ws[i + 1].text)
                if d2:
                    self.date2 = d2
                    i += 2
            elif d2 is not None:
                self.date2 = d2
                i += 1
        # remaining words: money vs description; merge a standalone '-' that
        # trails a money token (Navy Federal '1.00 -') or leads one
        # (Truist '- 300.00')
        rest = ws[i:]
        j = 0
        while j < len(rest):
            w = rest[j]
            tok = w.text
            if j + 1 < len(rest) and rest[j + 1].text == "-" \
               and (rest[j + 1].x0 - w.x1) < 14:
                mm = parse_money(tok + "-")
                if mm:
                    merged = Word(tok + "-", w.x0, rest[j + 1].x1, w.top, w.bottom, w.conf)
                    self.moneys.append((mm, merged))
                    j += 2
                    continue
            if tok == "-" and j + 1 < len(rest) \
               and (rest[j + 1].x0 - w.x1) < 10:
                mm = parse_money("-" + rest[j + 1].text)
                if mm:
                    nxt = rest[j + 1]
                    merged = Word("-" + nxt.text, w.x0, nxt.x1, nxt.top, nxt.bottom, nxt.conf)
                    self.moneys.append((mm, merged))
                    j += 2
                    continue
            mm = parse_money(tok)
            if mm:
                self.moneys.append((mm, w))
            else:
                # date tokens inside the row (e.g. settlement date) are not desc
                self.desc_words.append(w)
            j += 1

    @property
    def desc_text(self) -> str:
        return " ".join(w.text for w in self.desc_words)

    def desc_alpha(self) -> int:
        return sum(c.isalpha() for c in self.desc_text)


# ----------------------------------------------------------- the parser -----

class StatementParser:
    def __init__(self, pages_words: list[list[Word]], profile: Profile,
                 full_text: str):
        self.pages_words = pages_words
        self.profile = profile
        self.full_text = full_text
        self.period = find_period(full_text)
        self.section: str = ""
        self.section_dir: int = 0
        self.stopped = False
        self.stopped_page: Optional[int] = None
        self.total_pages = len(pages_words)
        self.blocks: list[AccountBlock] = []
        # (block, printed section name, printed amount) -- the block
        # reference is what came AT THE TIME each total was printed, so a
        # consolidated multi-account statement never checks one account's
        # printed total against another account's rows.
        self.section_totals: list[tuple["AccountBlock", str, Decimal]] = []
        self.warnings: list[str] = []

    # -- account blocks -----------------------------------------------------
    def _block(self) -> AccountBlock:
        if not self.blocks:
            self.blocks.append(AccountBlock(
                name="", is_credit_card=self.profile.is_credit_card))
        return self.blocks[-1]

    def _new_block(self, name: str, beginning: Optional[Decimal],
                   is_credit_card: Optional[bool] = None) -> None:
        cc = self.profile.is_credit_card if is_credit_card is None else is_credit_card
        b = self._block()
        # A block counts as "already started" only once it holds real
        # content (a transaction or a beginning balance) — NOT merely
        # because it has been named. An account_markers heading names the
        # block one line before the real 'Beginning Balance' row triggers
        # this same method again; treating the name alone as "started"
        # made that second call append a spurious empty duplicate block
        # instead of filling in the one just created.
        if b.txns or b.beginning is not None:
            self.blocks.append(AccountBlock(name=name, is_credit_card=cc))
        else:
            b.name = b.name or name
            b.is_credit_card = cc
        self._block().beginning = beginning

    def _drop(self, txn: Txn) -> None:
        for b in self.blocks:
            if txn in b.txns:
                b.txns.remove(txn)
                return

    # -- labelled balances ---------------------------------------------------
    # allows 'Ending Balance 37 $5,587.75' (instance count between) and
    # 'new balance as of 08/29/2025 = $12,851.34' (a date between), while
    # staying too narrow to jump across summary-table columns
    MONEY_AFTER = r"[^0-9$(]{0,4}.{0,18}?(\(?-?\$?\s?[\d,]+\.\d{2}\)?-?)"

    def _labelled_balance(self, text: str) -> Optional[tuple[str, Decimal]]:
        """Find 'Beginning balance ... 7,192.21' style lines. The value must
        FOLLOW the label on the same line — grabbing the last money on the
        line once captured a withdrawals total as an ending balance."""
        low = text.lower()
        for kind, labels in (("begin", self.profile.begin_labels),
                             ("end", self.profile.end_labels)):
            for lab in labels:
                m = re.search(lab + self.MONEY_AFTER, low)
                if m:
                    raw = m.group(1)
                    mm = parse_money(raw)
                    if mm:
                        return kind, mm.value * (mm.explicit_sign or 1)
        return None

    # -- column model per page ----------------------------------------------
    def _page_columns(self, lines: list[Line]) -> tuple[list[float], dict[float, str]]:
        """Cluster money right-edges on date-led lines; label via header words."""
        edges = []
        for ln in lines:
            rs = RowScan(ln)
            if rs.date and rs.moneys:
                edges.extend(w.x1 for _, w in rs.moneys)
        centers = cluster_right_edges(edges)
        labels: dict[float, str] = {}
        # Column labels are only meaningful on true multi-column layouts
        # (Wells Fargo: Credits | Debits | balance). On single-amount-column
        # statements, stray header words like a 'Deposits and Other
        # Additions' section title mislabel the one column and invert signs.
        n_amount_cols = len(centers) - (1 if self.profile.has_running_balance
                                        and len(centers) >= 2 else 0)
        if n_amount_cols < 2:
            return centers, labels
        # Labels must come from a genuine table-header line: one holding at
        # least two header words including a direction word. A section title
        # like 'Deposits and Other Additions' must not qualify — it once
        # relabelled the single amount column of a PNC statement as credits.
        DIRECTION = {"credits", "debits", "withdrawals", "balance"}
        headers: list[tuple[str, float]] = []
        for ln in lines:
            found = [(w.text.lower().strip("():"), (w.x0 + w.x1) / 2)
                     for w in ln.sorted_words()
                     if w.text.lower().strip("():") in HEADER_WORDS]
            names = {n for n, _ in found}
            if len(names) >= 2 and names & DIRECTION:
                headers.extend(found)
        for c in centers:
            best, bd = None, 1e9
            for name, hx in headers:
                d = abs(hx - c)
                if d < bd:
                    best, bd = name, d
            if best and bd < 80:
                labels[c] = best
        return centers, labels

    @staticmethod
    def _nearest_col(centers: list[float], x1: float) -> Optional[float]:
        best, bd = None, 1e9
        for c in centers:
            d = abs(c - x1)
            if d < bd:
                best, bd = c, d
        return best if bd <= COLUMN_TOL else None

    # -- direction ----------------------------------------------------------
    def _row_word_dir(self, desc: str) -> int:
        """Keyword match on the description. PDFs sometimes split words
        ('Withdr awal', 'De bit'), so we also match with spaces removed."""
        low = " " + desc.lower() + " "
        squashed = re.sub(r"\s+", "", low)

        def sq(pat: str) -> str:
            return re.sub(r"(\\b|\\s\+?|\s+)", "", pat)

        # Credit words first: 'Reversal: Monthly Service Fee' is a credit,
        # and checking debit words first flipped it (a real 30.00 error).
        for pat in self.profile.row_credit_words:
            if re.search(pat, low) or re.search(sq(pat), squashed):
                return 1
        for pat in self.profile.row_debit_words:
            if re.search(pat, low) or re.search(sq(pat), squashed):
                return -1
        return 0

    def _match_section(self, text: str) -> Optional[int]:
        t = text.strip()
        if len(t) > 110:      # headings can be glued to a table header row
            return None
        for pat in self.profile.credit_sections:
            if re.search(pat, t, re.I):
                return 1
        for pat in self.profile.debit_sections:
            if re.search(pat, t, re.I):
                return -1
        return None

    # -- main loop ----------------------------------------------------------
    def parse(self) -> list[AccountBlock]:
        pending: Optional[Txn] = None

        for pageno, words in enumerate(self.pages_words, start=1):
            if self.stopped:
                break
            lines = group_lines(words, pageno)
            centers, col_labels = self._page_columns(lines)
            balance_col = centers[-1] if (
                self.profile.has_running_balance and len(centers) >= 2) else None

            for ln in lines:
                text = ln.text
                if self.stopped:
                    break

                def clear_pending():
                    nonlocal pending
                    if pending is not None and pending.amount is None:
                        self._drop(pending)
                    pending = None

                marker_hit = False
                for pat in self.profile.stop_markers:
                    if re.search(pat, text, re.I):
                        marker_hit = True
                        break
                if marker_hit and self.profile.resume_markers \
                        and self._resume_found_after(pageno):
                    # a later page explicitly signals the real transaction
                    # table continues past this boilerplate (an issuer
                    # that interleaves a disclosure page before, not
                    # after, the rest of the statement) — this specific
                    # hit is waived rather than ending the document here;
                    # the boilerplate page itself has no dated rows to
                    # misread regardless
                    marker_hit = False
                if marker_hit:
                    self.stopped = True
                    self.stopped_page = pageno
                if self.stopped:
                    clear_pending()
                    break

                # A consolidated statement can print more than one account
                # in a single PDF (checking, then savings, each with its
                # own beginning/ending balance). account_markers names the
                # heading line that starts each one, so the previous
                # account's balance and sections never leak into the next.
                if self.profile.account_markers:
                    am = None
                    for pat in self.profile.account_markers:
                        am = re.match(pat, text)
                        if am:
                            break
                    if am:
                        name = am.group(1).strip()[:60] if am.groups() else text.strip()[:60]
                        if name != self._block().name:
                            self._new_block(name, None)
                            self.section, self.section_dir = "", 0
                            clear_pending()
                            continue

                sd = self._match_section(text)
                if sd is not None:
                    self.section, self.section_dir = text.strip()[:60], sd
                    clear_pending()
                    continue

                mt = SECTION_TOTAL_RE.match(text)
                if mt:
                    self.section_totals.append(
                        (self._block(), mt.group(1),
                         Decimal(mt.group(2).replace(",", ""))))
                    clear_pending()
                    continue

                rs = RowScan(ln)
                sq_text = re.sub(r"\s+", "", text)
                sq_desc = re.sub(r"\s+", "", rs.desc_text)

                # balance forward / beginning / ending rows are block events
                if BALANCE_FWD_RE.search(text) or BEGIN_ROW_SQ.match(sq_desc) \
                        or BEGIN_ROW_SQ.match(sq_text):
                    bal = rs.moneys[-1][0] if rs.moneys else None
                    amt = None
                    if bal:
                        amt = bal.value * (bal.explicit_sign or 1)
                    name = re.sub(r"\s+", " ", rs.desc_text)[:60]
                    # 'Beginning balance' is deposit-account vocabulary; card
                    # summaries say previous/new balance. In a credit card
                    # document this starts a deposit sub-account (e.g. the
                    # Chime secured account section).
                    self._new_block(name, amt, is_credit_card=False
                                    if self.profile.is_credit_card else None)
                    clear_pending()
                    continue
                if END_ROW_SQ.match(sq_desc) or END_ROW_SQ.match(sq_text):
                    if rs.moneys:
                        b = self._block()
                        m0 = rs.moneys[0][0]
                        b.ending = m0.value * (m0.explicit_sign or 1)
                    clear_pending()
                    continue
                # PNC-style balance summary: a header row naming both
                # 'Beginning ... Ending', values on the following line
                # ('1,381.87 8,450.00 10,228.17 396.30-')
                low = text.lower()
                if "beginning" in low and "ending" in low and not rs.moneys:
                    self._await_summary_values = True
                    clear_pending()
                    continue
                if getattr(self, "_await_summary_values", False) and \
                        len(rs.moneys) >= 3 and rs.date is None:
                    b = self._block()
                    m_first, m_last = rs.moneys[0][0], rs.moneys[-1][0]
                    if b.beginning is None:
                        b.beginning = m_first.value * (m_first.explicit_sign or 1)
                    if b.ending is None:
                        b.ending = m_last.value * (m_last.explicit_sign or 1)
                    self._await_summary_values = False
                    clear_pending()
                    continue
                if rs.moneys and rs.date is None:
                    self._await_summary_values = False

                # labelled balances live on non-transaction lines (summaries)
                if rs.moneys:
                    lab = self._labelled_balance(text)
                    is_txn_like = (rs.date is not None
                                   and rs.desc_alpha() >= MIN_DESC_ALPHA)
                    if lab and not is_txn_like:
                        kind, val = lab
                        b = self._block()
                        if kind == "begin" and b.beginning is None:
                            b.beginning = val
                        elif kind == "end" and b.ending is None:
                            b.ending = val
                        clear_pending()
                        continue

                # ---- transaction candidates --------------------------------
                if rs.date is None:
                    # continuation of the previous txn (wrapped description).
                    if pending is None or not (rs.desc_words or rs.moneys):
                        continue
                    if pending.amount is None:
                        # A wrapped row may carry its amount on the line
                        # immediately following the dated line — and only
                        # that one. If it is not there, the dated line was
                        # not a transaction; discard it (the balance chain
                        # and section totals catch anything real).
                        if rs.moneys:
                            pending.description = (pending.description + " "
                                                   + rs.desc_text).strip()
                            pending.raw_text += " | " + text
                            self._assign_amounts(pending, rs, centers,
                                                 col_labels, balance_col)
                            if pending.description and \
                                    sum(c.isalpha() for c in pending.description) \
                                    < MIN_DESC_ALPHA:
                                self._drop(pending)
                                pending = None
                        else:
                            self._drop(pending)
                            pending = None
                        continue
                    # normal wrapped text; may also carry the printed balance
                    pending.description += " " + rs.desc_text
                    pending.raw_text += " | " + text
                    if rs.moneys and balance_col is not None \
                            and pending.balance is None:
                        for m, w in rs.moneys:
                            if self._nearest_col(centers, w.x1) == balance_col:
                                pending.balance = m.value * (m.explicit_sign or 1)
                    continue

                # a new dated line supersedes an amount-less pending row
                if pending is not None and pending.amount is None:
                    self._drop(pending)
                    pending = None

                # some issuers only print transactions under known headings;
                # dated lines elsewhere are summary-box noise
                if self.profile.require_section and not self.section:
                    continue

                # structural rule: a real transaction has a description.
                # Daily-balance tables (a date and an amount on every row,
                # no description) die here.
                if rs.moneys and rs.desc_alpha() < MIN_DESC_ALPHA:
                    continue

                txn = Txn(date=None, date_raw=rs.date.raw,
                          description=re.sub(r"\s+", " ", rs.desc_text).strip(),
                          amount=None, balance=None, page=pageno,
                          raw_text=text, section=self.section)
                d = resolve_year(rs.date, self.period)
                txn.date = d
                if d is None:
                    txn.flag(f"Date '{rs.date.raw}' could not be placed in the "
                             f"statement period.")
                if rs.moneys:
                    self._assign_amounts(txn, rs, centers, col_labels, balance_col)
                self._block().txns.append(txn)
                pending = txn

        if pending is not None and pending.amount is None:
            self._drop(pending)
        self._add_cc_summary_charges()
        self._finish_signs()
        return self.blocks

    # a date-shaped token anywhere in a line, for the completeness check
    # below only — looser than core.parse_date_token (which validates a
    # whole token), because this only needs to rule out prose that merely
    # mentions a dollar amount (an arbitration clause capping filing fees
    # at '$700.00' is not a missed transaction)
    _DATE_IN_LINE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")

    def unread_tail_has_money(self) -> bool:
        """True if a stop marker fired before the last page AND a page after
        the stop point still contains what LOOKS like an unread transaction
        row: a date and a money figure on the same visual line, the same
        shape the engine itself requires to recognise a real row. A stop
        marker is meant to skip trailing legal boilerplate, not live
        transaction pages that happen to follow it in a long, bundled
        document — if this fires, completeness cannot be claimed and the
        caller must say so.

        Checking for ANY dollar figure in the tail (rather than one paired
        with a date) over-fires on legal boilerplate that happens to name a
        dollar amount ('reimburse arbitration filing fees up to $700.00'
        is prose, not a dropped row) and would otherwise mark a fully-read
        document unproven for no real reason."""
        if self.stopped_page is None or self.stopped_page >= self.total_pages:
            return False
        from .core import MONEY_IN_TEXT_RE, group_lines
        for pageno, words in enumerate(
                self.pages_words[self.stopped_page:], start=self.stopped_page + 1):
            for ln in group_lines(words, pageno):
                t = ln.text
                if self._DATE_IN_LINE_RE.search(t) and MONEY_IN_TEXT_RE.search(t):
                    return True
        return False

    def _resume_found_after(self, pageno: int) -> bool:
        """True if a profile.resume_markers pattern appears on a page
        strictly AFTER pageno (1-indexed). Used to waive a stop_markers
        hit when the issuer's own 'continued' language proves real
        content follows the boilerplate rather than ending there."""
        for words in self.pages_words[pageno:]:
            line_text = " ".join(w.text for w in words)
            for pat in self.profile.resume_markers:
                if re.search(pat, line_text, re.I):
                    return True
        return False

    def _add_cc_summary_charges(self) -> None:
        """Credit cards may print interest/fees only as summary lines
        ('Interest Charged +$73.72'), not as dated rows. Include them as
        transactions dated at period end — but ONLY when no dated
        interest/fee row exists (Chase prints a dated one; adding the
        summary too would double-count)."""
        if not self.profile.is_credit_card:
            return
        block = next((b for b in self.blocks if b.is_credit_card), None)
        if block is None:
            return
        for label, word in (("Interest Charged", r"interest"),
                            ("Fees Charged", r"fee")):
            # skip when a dated row of this kind already exists (Chase and
            # Capital One print dated fee rows; adding the summary too
            # would double-count). Also check the space-removed
            # description: some issuers' text layer letter-spaces words
            # ('I N T E R E S T C H A R G E'), which would otherwise defeat
            # this exact check and silently double the interest charge.
            def _has(t: Txn) -> bool:
                return bool(re.search(word, t.description, re.I)
                            or re.search(word, re.sub(r"\s+", "", t.description), re.I))
            if any(_has(t) for t in block.txns):
                continue
            for line in self.full_text.split("\n"):
                ls = line.strip()
                if not re.search(label + r"\b", ls, re.I):
                    continue
                # year-to-date summaries are not this period's charge
                if re.search(r"total\s+" + word + r"|year|in\s+20\d\d", ls, re.I):
                    continue
                m = re.search(label + self.MONEY_AFTER, ls, re.I)
                if not m:
                    continue
                mm = parse_money(m.group(1))
                if mm is None or mm.value == 0:
                    break
                t = Txn(date=self.period[1] if self.period else None,
                        date_raw="", description=label,
                        amount=mm.value,   # increases the amount owed
                        balance=None, page=0, raw_text=ls,
                        section=label, sign_source="summary-line")
                block.txns.append(t)
                break

    # -- amount assignment ---------------------------------------------------
    def _assign_amounts(self, txn: Txn, rs: RowScan, centers: list[float],
                        col_labels: dict[float, str],
                        balance_col: Optional[float]) -> None:
        amt: Optional[tuple[Money, str]] = None   # (money, source)
        for m, w in rs.moneys:
            col = self._nearest_col(centers, w.x1)
            if balance_col is not None and col == balance_col:
                if txn.balance is None:
                    txn.balance = m.value * (m.explicit_sign or 1)
                continue
            label = col_labels.get(col, "") if col else ""
            if amt is None:
                src = ""
                if m.explicit_sign is not None:
                    src = "explicit"
                elif label in ("credits", "deposits", "additions"):
                    src = "column:credit"
                elif label in ("debits", "withdrawals", "subtractions", "charges"):
                    src = "column:debit"
                amt = (m, src)
            # else: duplicated amount columns (Chime) — ignore extras
        if amt is None:
            txn.flag("No amount could be read for this transaction row.")
            return
        m, src = amt
        txn.amount = m.value          # magnitude for now; sign in _finish_signs
        txn.sign_source = src
        if m.explicit_sign is not None:
            txn.amount = m.value * m.explicit_sign
            txn.sign_source = "explicit"

    # -- sign resolution ----------------------------------------------------
    def _finish_signs(self) -> None:
        """Resolution order: explicit > column > row words > section > default.

        Internal amounts are the delta of the balance the statement
        reconciles: for a deposit account, money in is positive; for a credit
        card, the balance is money OWED, so a purchase is positive and a
        payment negative. Getting this wrong silently inverts a customer's
        money, so the mapping is explicit here.
        """
        for b in self.blocks:
            cc = b.is_credit_card
            for t in b.txns:
                if t.amount is None:
                    continue
                if t.sign_source == "explicit":
                    if cc:
                        t.amount = t.amount * self.profile.cc_explicit_orientation
                    continue
                direction = 0
                if t.sign_source == "column:credit":
                    direction = 1
                elif t.sign_source == "column:debit":
                    direction = -1
                elif self.profile.section_overrides_row_words and \
                        self._section_dir_for(t.section):
                    direction = self._section_dir_for(t.section)
                    t.sign_source = "section"
                else:
                    d = self._row_word_dir(t.description)
                    if d:
                        direction, t.sign_source = d, "row-words"
                    else:
                        sec_dir = self._section_dir_for(t.section)
                        if sec_dir:
                            direction, t.sign_source = sec_dir, "section"
                        elif self.profile.default_direction:
                            direction = self.profile.default_direction
                            t.sign_source = "default"
                if direction == 0:
                    t.sign_source = "unknown"
                    t.flag(f"Direction of {t.amount} could not be determined "
                           f"(no sign, no matching section or wording).")
                    continue
                # credit direction: money in for deposit accts (+),
                # debt reduction for credit cards (-)
                internal = direction if not cc else -direction
                t.amount = abs(t.amount) * internal

    def _section_dir_for(self, section: str) -> int:
        d = self._match_section(section) if section else None
        return d or 0


def pages_text(pages_words: list[list[Word]]) -> list[str]:
    texts = []
    for i, words in enumerate(pages_words, 1):
        lines = group_lines(words, i)
        texts.append("\n".join(ln.text for ln in lines))
    return texts


PAGE1_RE = re.compile(r"Page\s*1\s*of\s*\d+", re.I)


def split_statements(pages_words: list[list[Word]],
                     page_texts: Optional[list[str]] = None
                     ) -> list[tuple[list[list[Word]], str]]:
    """One PDF is not always one statement: bundles carry several statement
    periods, each with its own opening and closing balance that must be
    reconciled separately. A page reading 'Page 1 of N' starts a new one.

    page_texts, when given, must be the verbatim per-page text pdfplumber
    itself produced (pdftext.extract_native's 4th return value). Chunk text
    is built from those originals rather than reconstructed via
    group_lines(), so a single-statement file (the overwhelming majority)
    parses byte-for-byte the same as it always has — the reconstruction is
    close but not perfect, and the gap can silently drop a summary line
    (e.g. an 'Interest Charged' total) that the original text matched.
    """
    texts = page_texts if page_texts is not None else pages_text(pages_words)
    starts = [i for i, t in enumerate(texts) if PAGE1_RE.search(t)]
    if not starts or starts[0] != 0:
        starts = [0] + [s for s in starts if s != 0]
    chunks = []
    for j, s in enumerate(starts):
        e = starts[j + 1] if j + 1 < len(starts) else len(pages_words)
        chunks.append((pages_words[s:e], "\n".join(texts[s:e])))
    return chunks


def parse_statement(pages_words: list[list[Word]], full_text: str,
                    profile: Optional[Profile] = None
                    ) -> tuple[Profile, int, list[AccountBlock], "StatementParser"]:
    if profile is None:
        head = full_text[:6000]
        profile, conf = detect_profile(head)
    else:
        conf = 97
    sp = StatementParser(pages_words, profile, full_text)
    blocks = sp.parse()
    return profile, conf, blocks, sp
