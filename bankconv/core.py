"""Core data model, money tokens, dates, and line geometry.

Design notes (from the build brief):
- Money tokens are recognised by shape; columns are found by clustering the
  right-hand edges of money tokens, never by per-bank layout regexes.
- Year patterns must not match inside money amounts.
- Year-less date formats are first-class.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

CENT = Decimal("0.01")

# ---------------------------------------------------------------- money -----

# A money-shaped token: optional $, optional leading sign or paren, digits with
# optional thousands separators, mandatory cents, optional trailing minus.
# The bare '.68' (no leading zero) alternative exists because at least one
# issuer (Chase Marriott Bonvoy) prints sub-dollar amounts without the
# leading 0 ('05/10 WALMART.COM 800-925-6278 AR .68') — pdfplumber hands
# that back as its own isolated word token, so this is never a stray
# fragment of a larger number the way it could be if matched inside free
# running text (see MONEY_IN_TEXT_RE below, which is deliberately NOT
# widened the same way). Missing this dropped one real transaction with no
# flag or warning at all — caught only because reconciliation was off by
# exactly that amount.
MONEY_RE = re.compile(
    r"""^\(?          # optional opening paren (accounting negative)
        (?P<lead>[-+])?\s*
        \$?\s*
        (?P<num>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2}|\.\d{2})
        \)?\s*
        (?P<trail>-)?$""",
    re.VERBOSE,
)

MONEY_IN_TEXT_RE = re.compile(
    r"[-+]?\$?\s?\d{1,3}(?:,\d{3})*\.\d{2}-?|\(\$?\d{1,3}(?:,\d{3})*\.\d{2}\)"
)


@dataclass
class Money:
    value: Decimal          # magnitude, always >= 0
    explicit_sign: Optional[int]  # -1, +1, or None if the token itself is unsigned
    raw: str


def parse_money(tok: str) -> Optional[Money]:
    tok = tok.strip()
    m = MONEY_RE.match(tok)
    if not m:
        return None
    try:
        val = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None
    sign: Optional[int] = None
    if m.group("trail") == "-" or m.group("lead") == "-" or tok.startswith("("):
        sign = -1
    elif m.group("lead") == "+":
        sign = 1
    return Money(val, sign, tok)


# ---------------------------------------------------------------- dates -----

# Year tokens: exclude digits, commas and periods on BOTH sides so the "2000"
# inside 2,000.00 can never match (previous build dated a whole file to 2000).
YEAR_RE = re.compile(r"(?<![\d,.\-/])(19|20)\d{2}(?![\d,.\-/])")

MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
MONTHS.update({m[:3].lower(): i for m, i in list(MONTHS.items())})

# Date token patterns, tried in order. Year-less forms are required support.
_DATE_PATTERNS = [
    # 01/31/2022, 1-31-22
    re.compile(r"^(?P<m>\d{1,2})[/-](?P<d>\d{1,2})[/-](?P<y>\d{2,4})$"),
    # 01/31, 01-31
    re.compile(r"^(?P<m>\d{1,2})[/-](?P<d>\d{1,2})$"),
]
_MONTHNAME = re.compile(r"^(?P<mon>[A-Za-z]{3,9})\.?$")


@dataclass
class DateToken:
    month: int
    day: int
    year: Optional[int]   # None when the statement omits it
    raw: str


def parse_date_token(tok: str) -> Optional[DateToken]:
    orig = tok.strip()
    # a trailing footnote marker glued to the date ('12/16/25*' meaning
    # "posting date", per Amex's own key) must not defeat date recognition
    # — the alternative is silently dropping the whole row, amount and all
    tok = orig.rstrip(":*")
    for pat in _DATE_PATTERNS:
        m = pat.match(tok)
        if m:
            mo, dy = int(m.group("m")), int(m.group("d"))
            if not (1 <= mo <= 12 and 1 <= dy <= 31):
                return None
            yr = None
            if "y" in m.groupdict() and m.group("y"):
                yr = int(m.group("y"))
                if yr < 100:
                    yr += 2000
            return DateToken(mo, dy, yr, orig)
    return None


def parse_monthname_date(tok1: str, tok2: str) -> Optional[DateToken]:
    """'Nov 15' or 'Nov 15,' as two adjacent tokens."""
    m = _MONTHNAME.match(tok1)
    if not m:
        return None
    mon = MONTHS.get(m.group("mon").lower())
    if not mon:
        return None
    t2 = tok2.rstrip(",.")
    if not t2.isdigit():
        return None
    dy = int(t2)
    if not (1 <= dy <= 31):
        return None
    return DateToken(mon, dy, None, f"{tok1} {tok2}")


def resolve_year(dt: DateToken, period: tuple[date, date] | None) -> Optional[date]:
    """Pick the year so the date lands inside (or near) the statement period.

    Handles Dec->Jan rollover: on a statement covering Dec 2022 - Jan 2023 a
    bare '01/03' resolves to 2023 and '12/17' to 2022.
    """
    if dt.year is not None:
        try:
            return date(dt.year, dt.month, dt.day)
        except ValueError:
            return None
    if period is None:
        return None
    lo = period[0] - timedelta(days=45)
    hi = period[1] + timedelta(days=45)
    for y in {period[0].year, period[1].year, period[0].year - 1, period[1].year + 1}:
        try:
            cand = date(y, dt.month, dt.day)
        except ValueError:
            continue
        if lo <= cand <= hi:
            return cand
    return None


# -------------------------------------------------------------- geometry ----

@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    conf: Optional[float] = None   # OCR confidence 0-100, None for native text


@dataclass
class Line:
    words: list[Word]
    page: int

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in sorted(self.words, key=lambda w: w.x0))

    def sorted_words(self) -> list[Word]:
        return sorted(self.words, key=lambda w: w.x0)


def estimate_skew(words: list[Word]) -> float:
    """Slope of word baselines vs x, applied ONLY when the correlation is
    strong. A straight page full of scattered text produces a weak, noisy
    slope; shearing by that noise scrambles line grouping (it silently
    interleaved rows of a perfectly straight Chase statement in testing).
    Real skew (a rotated scan) shows a consistent slope with high R^2."""
    if len(words) < 40:
        return 0.0
    xs = [(w.x0 + w.x1) / 2 for w in words]
    ys = [w.bottom for w in words]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx < 1e-6 or syy < 1e-6:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    if r2 < 0.5 or abs(slope) > 0.25:
        return 0.0
    return slope


def group_lines(words: list[Word], page: int) -> list[Line]:
    """Group words into visual lines, correcting page skew first.

    Naive grouping shatters on a rotated page, so we subtract the estimated
    baseline slope before bucketing by vertical position.
    """
    if not words:
        return []
    slope = estimate_skew(words)
    # de-skewed vertical position
    def vpos(w: Word) -> float:
        return w.top - slope * ((w.x0 + w.x1) / 2)

    heights = sorted(w.bottom - w.top for w in words)
    med_h = heights[len(heights) // 2] or 8.0
    tol = max(2.0, med_h * 0.45)

    # gap-based clustering on the sorted vertical positions; no running-mean
    # drift (drift merged adjacent rows on tightly printed statements)
    ordered = sorted(words, key=vpos)
    lines: list[Line] = []
    cur: list[Word] = []
    prev_v = None
    for w in ordered:
        v = vpos(w)
        if cur and prev_v is not None and (v - prev_v) > tol:
            lines.append(Line(cur, page))
            cur = []
        cur.append(w)
        prev_v = v
    if cur:
        lines.append(Line(cur, page))
    return lines


def cluster_right_edges(edges: list[float], tol: float = 6.0) -> list[float]:
    """Cluster x1 positions of money tokens; returns cluster centers sorted
    left-to-right. This is the geometric column finder."""
    if not edges:
        return []
    edges = sorted(edges)
    clusters: list[list[float]] = [[edges[0]]]
    for e in edges[1:]:
        if e - clusters[-1][-1] <= tol:
            clusters[-1].append(e)
        else:
            clusters.append([e])
    # keep clusters with enough members to be a column (>=2) OR all if few
    centers = [sum(c) / len(c) for c in clusters if len(c) >= 2]
    if not centers:
        centers = [sum(c) / len(c) for c in clusters]
    return sorted(centers)


# ---------------------------------------------------------------- rows ------

@dataclass
class Txn:
    date: Optional[date]
    date_raw: str
    description: str
    amount: Optional[Decimal]        # signed; positive = money in
    balance: Optional[Decimal]
    page: int
    sign_source: str = ""            # explicit / column / row-words / section / default
    status: str = "OK"
    review_note: str = ""
    raw_text: str = ""
    section: str = ""
    conf: Optional[float] = None

    def flag(self, note: str) -> None:
        self.status = "NEEDS_REVIEW"
        self.review_note = (self.review_note + " " + note).strip()


@dataclass
class AccountBlock:
    name: str
    beginning: Optional[Decimal] = None
    ending: Optional[Decimal] = None
    txns: list[Txn] = field(default_factory=list)
    is_credit_card: bool = False
    recon_passed: Optional[bool] = None
    recon_detail: str = ""
