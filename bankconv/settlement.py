"""Settlement statement parsers.

Two formats in the wild set:
- Colonial Van Lines driver settlements: per-job COD / total cost /
  deductions / commission / add-backs with printed subtotals, then a
  summary of global adjustments ending in TOTAL PAY TO DRIVER. The printed
  subtotal chain makes the whole document provable by arithmetic.
- Jordan Carriers deduction history report: a flat ledger of deduction (D)
  and earning (E) rows grouped by code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

TOL = Decimal("0.01")
MONEY = r"-?\$[\d,]+\.\d{2}"


def _dec(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", ""))


@dataclass
class SettleRow:
    section: str
    job: Optional[str]
    customer: Optional[str]
    rtype: str
    description: str
    amount: Decimal
    status: str = "OK"
    review_note: str = ""
    raw: str = ""

    def flag(self, note: str) -> None:
        self.status = "NEEDS_REVIEW"
        self.review_note = (self.review_note + " " + note).strip()


@dataclass
class SettleResult:
    rows: list[SettleRow] = field(default_factory=list)
    recon_passed: Optional[bool] = None
    recon_detail: str = ""
    display: str = ""
    key: str = ""
    driver: str = ""


def detect_settlement(text: str) -> Optional[str]:
    if re.search(r"TOTAL PAY TO DRIVER", text) and re.search(r"COD\s+Job\s?#", text):
        return "colonial_van_lines_settlement"
    if re.search(r"Settlement Deduction History Report", text):
        return "jordan_carriers_deduction_report"
    if re.search(r"Owner Operator Settlement Summary", text) \
            and re.search(r"Shelton Trucking", text, re.I):
        return "shelton_trucking_settlement"
    return None


# ------------------------------------------------------------- Colonial ----

# NOTE: amounts and descriptions can be glued in the text layer
# ('-$1,002.00Compliance'), so separators are \s* not \s+. Some lines carry
# a second Comdata-breakdown column; the amount is the FIRST money after
# the description and trailing columns are ignored.
JOB_FIRST = re.compile(
    r"^(?P<cust>.+?)\s+(?P<cod>" + MONEY + r")\s+(?P<job>\S+?-CV)\s+"
    r"(?P<cost>" + MONEY + r")\s*(?P<costlabel>Total Cost|Driver Pay)\s*$")
DEDUCT = re.compile(r"^(?P<amt>-\$[\d,]+\.\d{2})\s*(?P<desc>\S.*)$")
SUBTOT = re.compile(r"^(?P<amt>\$[\d,]+\.\d{2})\s*$")
ADDBACK = re.compile(r"^(?P<amt>\$[\d,]+\.\d{2})\s*(?P<desc>\S.*)$")
COMMISSION = re.compile(r"^(?P<amt>-\$[\d,]+\.\d{2})\s*(?P<desc>CVL\s*\d+%.*)$")
TOTAL_JOBS = re.compile(r"^Total Jobs:\s*(?P<amt>" + MONEY + r")(?P<rest>.*)$")
TOTAL_PAY = re.compile(r"^TOTAL PAY TO DRIVER\s+(?P<amt>" + MONEY + r")\s*$")
ADJUST = re.compile(r"^(?P<desc>.+?)\s*(?P<amt>" + MONEY + r")(?:\s+.*)?$")
NEG_SETTLE = re.compile(r"^(?P<desc>Negative Settlement\s*[\d/]*)\s*"
                        r"(?P<amt>" + MONEY + r")(?:\s+.*)?$")
DRIVER = re.compile(r"^(?P<name>[A-Za-z .'-]+)\s*\((?P<date>[\d/]+)\)\s*$")


def parse_colonial(text: str) -> SettleResult:
    res = SettleResult(display="Colonial Van Lines Settlement Statement",
                       key="colonial_van_lines_settlement")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if lines:
        m = DRIVER.match(lines[0])
        if m:
            res.driver = f"{m.group('name').strip()} ({m.group('date')})"

    in_summary = False
    cur_job: Optional[str] = None
    cur_cust: Optional[str] = None
    # arithmetic chain within a job
    chain: Optional[Decimal] = None
    chain_rows: list[SettleRow] = []
    job_nets: list[Decimal] = []
    total_jobs_printed: Optional[Decimal] = None
    total_pay_printed: Optional[Decimal] = None
    stopped = False

    def check_subtotal(printed: Decimal, row: SettleRow) -> None:
        nonlocal chain
        if chain is None:
            chain = printed
            return
        if abs(chain - printed) > TOL:
            for r in chain_rows:
                r.flag(f"The rows above sum to {chain:,.2f} but the printed "
                       f"subtotal is {printed:,.2f}.")
            row.flag(f"Printed subtotal {printed:,.2f} does not match the "
                     f"sum of the rows above ({chain:,.2f}).")
        chain = printed
        chain_rows.clear()

    trailer_from: int | None = None
    for idx, ln in enumerate(lines):
        if stopped:
            break
        if re.match(r"^\*\*", ln):
            # Trailer tables ('ESTIMATED REPAIRS FOR ACCIDENT', 'MISSING
            # DELIVERY DOCS'). They are not settlement line items and must
            # not enter the arithmetic, but they ARE content on the page,
            # so they are captured in their own sections below.
            trailer_from = idx
            break
        m = TOTAL_PAY.match(ln)
        if m:
            total_pay_printed = _dec(m.group("amt"))
            res.rows.append(SettleRow("Summary", None, None, "Total",
                                      "TOTAL PAY TO DRIVER",
                                      total_pay_printed, raw=ln))
            stopped = True
            continue
        m = TOTAL_JOBS.match(ln)
        if m:
            in_summary = True
            total_jobs_printed = _dec(m.group("amt"))
            res.rows.append(SettleRow("Summary", None, None, "Subtotal",
                                      "Total Jobs", total_jobs_printed, raw=ln))
            res.rows.extend(_comdata_extras(m.group("rest"), ln))
            continue
        m = NEG_SETTLE.match(ln)
        if m:
            res.rows.append(SettleRow("Summary", None, None, "Adjustment",
                                      m.group("desc").strip(),
                                      _dec(m.group("amt")), raw=ln))
            continue
        if not in_summary:
            m = JOB_FIRST.match(ln)
            if m:
                # close the previous job
                if chain is not None:
                    job_nets.append(chain)
                cur_cust = m.group("cust").strip()
                cur_job = m.group("job")
                cod = _dec(m.group("cod"))
                cost = _dec(m.group("cost"))
                label = m.group("costlabel")
                res.rows.append(SettleRow("Job", cur_job, cur_cust, "COD",
                                          "Cash on delivery", cod, raw=ln))
                r = SettleRow("Job", cur_job, cur_cust, label,
                              label.replace("Total Cost", "Total cost"),
                              cost, raw=ln)
                res.rows.append(r)
                chain = cost
                chain_rows = [r]
                continue
            if cur_job:
                m = COMMISSION.match(ln)
                if m:
                    amt = _dec(m.group("amt"))
                    r = SettleRow("Job", cur_job, cur_cust, "Commission",
                                  m.group("desc").strip(), amt, raw=ln)
                    res.rows.append(r)
                    if chain is not None:
                        chain += amt
                    chain_rows.append(r)
                    continue
                m = DEDUCT.match(ln)
                if m:
                    amt = _dec(m.group("amt"))
                    r = SettleRow("Job", cur_job, cur_cust, "Deduction",
                                  m.group("desc").strip(), amt, raw=ln)
                    res.rows.append(r)
                    if chain is not None:
                        chain += amt
                    chain_rows.append(r)
                    continue
                m = SUBTOT.match(ln)   # a bare amount is a printed subtotal
                if m:
                    printed = _dec(m.group("amt"))
                    r = SettleRow("Job", cur_job, cur_cust, "Subtotal",
                                  "Subtotal", printed, raw=ln)
                    res.rows.append(r)
                    check_subtotal(printed, r)
                    continue
                m = ADDBACK.match(ln)   # positive line inside a job
                if m:
                    amt = _dec(m.group("amt"))
                    r = SettleRow("Job", cur_job, cur_cust, "Add-back",
                                  m.group("desc").strip(), amt, raw=ln)
                    res.rows.append(r)
                    if chain is not None:
                        chain += amt
                    chain_rows.append(r)
                    continue
        else:
            m = ADJUST.match(ln)
            if m and not re.search(r"Job\s?#$", m.group("desc")):
                desc = m.group("desc").strip()
                amt = _dec(m.group("amt"))
                # 'Uhaul ... …Holding $111.98 $0.00' prints the held amount
                # followed by the APPLIED amount; the applied one counts.
                if re.search(r"holding", desc, re.I):
                    moneys = re.findall(MONEY, ln)
                    if len(moneys) >= 2:
                        amt = _dec(moneys[-1])
                        desc += f" (held: {moneys[0]})"
                res.rows.append(SettleRow("Summary", None, None, "Adjustment",
                                          desc, amt, raw=ln))
                tail = ln[m.end("amt"):] if m.end("amt") < len(ln) else ""
                res.rows.extend(_comdata_extras(tail, ln))
                continue

    if chain is not None and not in_summary:
        job_nets.append(chain)
    elif chain is not None and in_summary and total_jobs_printed is not None:
        job_nets.append(chain)

    # The trailer sits after TOTAL PAY TO DRIVER, which already stops the
    # settlement scan, so locate it independently rather than relying on
    # the loop reaching it.
    if trailer_from is None:
        for idx, ln in enumerate(lines):
            if re.match(r"^\*\*", ln):
                trailer_from = idx
                break
    if trailer_from is not None:
        res.rows.extend(_parse_trailer(lines[trailer_from:]))

    # ---- file-level arithmetic ------------------------------------------
    notes = []
    ok = True
    if total_jobs_printed is not None and job_nets:
        s = sum(job_nets)
        if abs(s - total_jobs_printed) > TOL:
            ok = False
            notes.append(f"The job nets sum to {s:,.2f} but the statement "
                         f"prints Total Jobs {total_jobs_printed:,.2f}.")
    if total_pay_printed is not None and total_jobs_printed is not None:
        adjustments = sum(r.amount for r in res.rows
                          if r.section == "Summary" and r.rtype == "Adjustment")
        expect = total_jobs_printed + adjustments
        if abs(expect - total_pay_printed) > TOL:
            ok = False
            notes.append(
                f"Total Jobs {total_jobs_printed:,.2f} plus adjustments "
                f"{adjustments:,.2f} gives {expect:,.2f}, but the statement "
                f"prints TOTAL PAY TO DRIVER {total_pay_printed:,.2f}.")
    if total_pay_printed is None:
        ok = False
        notes.append("No TOTAL PAY TO DRIVER line was found.")
    subtot_flags = any(r.status != "OK" for r in res.rows)
    res.recon_passed = ok and not subtot_flags
    if res.recon_passed:
        res.recon_detail = (
            "Every printed subtotal matches the rows above it, the job nets "
            f"sum to Total Jobs {total_jobs_printed:,.2f}, and Total Jobs "
            "plus adjustments equals TOTAL PAY TO DRIVER "
            f"{total_pay_printed:,.2f}.")
    else:
        res.recon_detail = " ".join(notes) if notes else \
            "One or more printed subtotals do not match their rows; see the marked rows."
    return res


COMDATA_ENTRY = re.compile(r"(?P<amt>" + MONEY + r")(?:\s+(?P<job>\S+?-CV))?")


def _comdata_extras(tail: str, raw: str) -> list[SettleRow]:
    """Colonial prints a second 'Comdata Breakdown' column to the right of
    the summary. Those figures belong to the page, so they are captured in
    their own section rather than folded into the settlement arithmetic."""
    if not tail or not tail.strip():
        return []
    out = []
    for m in COMDATA_ENTRY.finditer(tail):
        job = m.group("job")
        out.append(SettleRow("Comdata Breakdown", job, None, "Entry",
                             f"Comdata breakdown{' for ' + job if job else ''}",
                             _dec(m.group("amt")), raw=raw))
    return out


TRAILER_HEAD = re.compile(r"^\*\*(?P<name>.+?)\*\*\s*$")
TRAILER_MONEY = re.compile(r"^(?P<desc>.+?)\s+(?P<amt>" + MONEY + r")\s*$")
TRAILER_JOB = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\s+(?P<job>\S+?-CV)\s*(?P<note>.*)$")


def _parse_trailer(lines: list[str]) -> list[SettleRow]:
    """Capture the tables printed after TOTAL PAY TO DRIVER.

    These are notes to the driver (accident repair schedules, missing
    paperwork), not settlement line items, so they are recorded in their
    own sections and deliberately excluded from the settlement
    arithmetic. Nothing printed on the page is dropped.
    """
    out: list[SettleRow] = []
    section = "Notes"
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        mh = TRAILER_HEAD.match(ln)
        if mh:
            section = mh.group("name").strip().title()
            continue
        if re.match(r"^Settlement Week\s+Job\s?#$", ln, re.I):
            continue
        mj = TRAILER_JOB.match(ln)
        if mj:
            note = mj.group("note").strip()
            out.append(SettleRow(section, mj.group("job"), None, "Note",
                                 f"{mj.group('date')}"
                                 + (f" {note}" if note else ""),
                                 Decimal("0.00"), raw=ln))
            continue
        mm = TRAILER_MONEY.match(ln)
        if mm:
            desc = mm.group("desc").strip()
            kind = ("Opening" if re.match(r"settlement week", desc, re.I)
                    else "Balance" if re.match(r"balance", desc, re.I)
                    else "Payment")
            out.append(SettleRow(section, None, None, kind, desc,
                                 _dec(mm.group("amt")), raw=ln))
            continue
        out.append(SettleRow(section, None, None, "Note", ln,
                             Decimal("0.00"), raw=ln))
    return out


# --------------------------------------------------------------- Jordan ----

# The D/E type letter is unreliable in the text layer: overlapping text
# runs interleave it into the middle of words ('MAX MEADOWS DVA'). The row
# anchor is payee + date + trailing amount; direction comes from the
# amount's own printed sign, which the count/sum checks then prove.
JORDAN_ROW = re.compile(
    r"^(?P<order>\d{5,9}\s+)?(?P<payee>\S{2,8})\s+(?P<name>.+?)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<amt>-?[\d,]+\.\d{2})\s*$")
JORDAN_CODE_TOTAL = re.compile(
    r"Deduct/Earning code\s+(?P<code>\S+)\s+totals?:\s*(?P<n>\d+)\s+Record",
    re.I)
JORDAN_REPORT_TOTAL = re.compile(
    r"Report totals?:\s*(?P<n>\d+)\s+Record", re.I)


def parse_jordan(text: str) -> SettleResult:
    """The report prints, for every deduction/earning code, a totals line
    with a record count and a sum, plus a report-level total. Those printed
    figures are the proof mechanism: our extracted rows must match both the
    count and the sum, per code and overall."""
    res = SettleResult(display="Jordan Carriers Settlement Deduction Report",
                       key="jordan_carriers_deduction_report")
    section = ""
    sec_rows: list[SettleRow] = []
    notes: list[str] = []
    checked = 0
    report_total_seen = False

    def money_on(ln: str) -> Optional[Decimal]:
        clean = ln.replace("_", "")
        m = re.search(r"(-?\$?[\d,]+\.\d{2})\s*$", clean)
        return _dec(m.group(1)) if m else None

    for raw_ln in text.split("\n"):
        ln = raw_ln.strip()
        if not ln:
            continue
        mt = JORDAN_CODE_TOTAL.search(ln)
        if mt:
            printed_n = int(mt.group("n"))
            printed_sum = money_on(ln)
            s = sum(r.amount for r in sec_rows)
            checked += 1
            if len(sec_rows) != printed_n:
                for r in sec_rows:
                    r.flag(f"The report prints {printed_n} records for code "
                           f"{mt.group('code')} but {len(sec_rows)} were read.")
                notes.append(f"code {mt.group('code')}: {len(sec_rows)} rows "
                             f"read, {printed_n} printed")
            elif printed_sum is not None and abs(s - printed_sum) > TOL:
                for r in sec_rows:
                    r.flag(f"The {printed_n} rows of code {mt.group('code')} "
                           f"sum to {s:,.2f} but the report prints "
                           f"{printed_sum:,.2f}.")
                notes.append(f"code {mt.group('code')}: sum {s:,.2f} vs "
                             f"printed {printed_sum:,.2f}")
            sec_rows = []
            continue
        mrt = JORDAN_REPORT_TOTAL.search(ln)
        if mrt:
            report_total_seen = True
            printed_n = int(mrt.group("n"))
            printed_sum = money_on(ln)
            s = sum(r.amount for r in res.rows)
            if len(res.rows) != printed_n:
                notes.append(f"report: {len(res.rows)} rows read but the "
                             f"report prints {printed_n} records")
            elif printed_sum is not None and abs(s - printed_sum) > TOL:
                notes.append(f"report: rows sum to {s:,.2f} but the report "
                             f"prints {printed_sum:,.2f}")
            continue
        m = JORDAN_ROW.match(ln)
        if m and not ln.startswith(("Check date", "Deduction/Earning")):
            amt = _dec(m.group("amt"))
            r = SettleRow(section or "Deductions", m.group("payee"),
                          None,
                          "Deduction" if amt < 0 else "Earning",
                          f"{m.group('name').strip()} {m.group('date')}",
                          amt, raw=ln)
            res.rows.append(r)
            sec_rows.append(r)
            continue
        mh = re.match(r"^([A-Z0-9]{2,6})\s+([A-Z][A-Z0-9 /&'.-]+)$", ln)
        if mh and not sec_rows:
            section = f"{mh.group(1)} {mh.group(2).title()}"
            continue

    if not res.rows:
        res.recon_passed = None
        res.recon_detail = "No rows were recognised."
        return res
    ok = not notes and checked > 0 and report_total_seen
    res.recon_passed = ok if (checked or report_total_seen) else None
    if ok:
        res.recon_detail = (
            f"All {checked} printed code totals and the report total "
            f"({len(res.rows)} records) match the extracted rows in both "
            f"count and sum.")
    else:
        res.recon_detail = ("Mismatches against printed totals: "
                            + "; ".join(notes) if notes else
                            "No printed totals found to check against.")
    return res


# -------------------------------------------------------------- Shelton ----

# Shelton Trucking / P&S Logistics "Owner Operator Settlement Summary".
# Three per-load tables -> a driver-level fuel/DEF deduction ledger -> a
# recurring lease-escrow deduction -> itemized misc deductions and
# reimbursements -> a PAY SUMMARY that reconciles to the final NET PAY -> an
# escrow-account activity ledger. Every stage is independently provable by
# arithmetic against the previous one.
#
# The PDF underlines the last row before every printed subtotal, and the
# text layer renders that as '_' characters interleaved character-by-
# character with the digits ('17.560___-_$_1_7._5_6_'). Every money-bearing
# pattern below tolerates '_' (and, on the labeled summary lines, embedded
# spaces too) inside the amount and strips them before parsing Decimal --
# the digits themselves are never touched, only the decorative glyphs
# around them.

SHELTON_SETTLEMENT_HDR = re.compile(r"^SETTLEMENT\s+(?P<num>\S+)$")
SHELTON_LOAD_ROW = re.compile(
    r"^(?P<route>.+?)\s+Loaded\s+(?P<miles>[\d.]+)\s+(?P<tractor>\S+)\s+"
    r"(?P<shipdt>\d{2}/\d{2}/\d{2})\s+(?P<deliverydt>\d{2}/\d{2}/\d{2})\s+"
    r"\$(?P<gross>[\d,]+\.\d{2})\s+(?P<rate>[\d.]+)%\s+\$(?P<net>[\d,]+\.\d{2})$")
SHELTON_TYPED_ROW = re.compile(
    r"^(?P<type>Earning|Reimbursement|Deduction)\s+(?P<desc>.+?)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<unit>[\d.]+)\s+(?P<rate>[\d.]+)\s*"
    r"(?P<amt>[-$_\d,.]+)$")
SHELTON_RECURRING_ROW = re.compile(
    r"^(?P<type>Earning|Reimbursement|Deduction)\s+(?P<desc>.+?)\s+"
    r"(?P<base>[\d,]+\.\d{2})\s+(?P<method>.+?)\s+(?P<amt>[-$_\d,.]+)$")
SHELTON_PLAIN_ROW = re.compile(
    r"^(?P<desc>.+?)\s+(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<unit>[\d.]+)\s+"
    r"(?P<rate>[\d.]+)\s*(?P<amt>[-$_\d,.]+)$")
SHELTON_ORDER_TOTAL = re.compile(r"^ORDER TOTAL\s+(?P<amt>.+)$")
SHELTON_LABELED = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9 /&.,'()-]*?):\s*(?P<amt>.+)$")
SHELTON_ACCOUNT_HDR = re.compile(r"^\d[\d.]+\s+(?P<name>.+)$")


def _shelton_clean_amt(raw: str) -> Optional[Decimal]:
    """Strip decorative underline underscores (and, on labeled summary
    lines, stray spaces the underline injects) before parsing. Returns
    None if what's left doesn't look like a dollar amount at all, so
    non-money lines (mileage counts, etc.) are never mistaken for one."""
    s = re.sub(r"[_\s]", "", raw)
    if not re.match(r"^-?\$?[\d,]+\.\d{2}$", s):
        return None
    return _dec(s)


def parse_shelton(text: str) -> SettleResult:
    res = SettleResult(display="Shelton Trucking Owner Operator Settlement",
                       key="shelton_trucking_settlement")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    m = re.search(r"For\s+\S+\s+(?P<name>[A-Za-z ,.'-]+?)\s+Email:", text)
    if m:
        res.driver = m.group("name").strip()

    section = ""
    cur_order: Optional[str] = None
    order_net: Optional[Decimal] = None
    order_extra = Decimal("0")
    order_nets: list[Decimal] = []
    order_totals: list[Decimal] = []
    driver_deduct_rows: list[SettleRow] = []
    recurring_rows: list[SettleRow] = []
    deduction_rows: list[SettleRow] = []
    reimbursement_rows: list[SettleRow] = []
    earning_rows: list[SettleRow] = []
    reimb_order_rows: list[SettleRow] = []
    dd_subtotal: Optional[Decimal] = None
    recurring_subtotal: Optional[Decimal] = None
    deductions_subtotal: Optional[Decimal] = None
    reimbursements_subtotal: Optional[Decimal] = None
    driver_subtotal_vals: dict[str, Decimal] = {}
    pay_summary_vals: dict[str, Decimal] = {}
    escrow_accounts: list[dict] = []
    notes: list[str] = []

    def close_order() -> None:
        nonlocal cur_order, order_net, order_extra
        if cur_order is not None and order_net is not None:
            order_nets.append(order_net)
        cur_order = None
        order_net = None
        order_extra = Decimal("0")

    for raw_ln in lines:
        ln = raw_ln
        if re.match(r"^\d{2}/\d{2}/\d{4}\s+\d{3,4}\s+Owner Operator "
                    r"Settlement Summary", ln) or ln == "Shelton Trucking" \
           or re.match(r"^P\.O\. Box.*Phone:", ln) \
           or re.match(r"^Check #\s*:", ln) \
           or re.match(r"^Pay period:", ln) \
           or re.match(r"^For\s+\S+.*Email:", ln) \
           or re.match(r"^Driver paid at", ln) \
           or re.match(r"^Origin Destination Loaded", ln) \
           or re.match(r"^Type Description", ln) \
           or re.match(r"^Order Number Description", ln) \
           or re.match(r"^_+$", ln) \
           or re.match(r"^Remember to scan", ln) \
           or re.match(r"^send your paperwork", ln):
            continue

        m = SHELTON_SETTLEMENT_HDR.match(ln)
        if m:
            close_order()
            cur_order = m.group("num")
            section = "order"
            continue

        m = SHELTON_ORDER_TOTAL.match(ln)
        if m:
            amt = _shelton_clean_amt(m.group("amt"))
            if amt is not None:
                order_totals.append(amt)
                r = SettleRow("Load", cur_order, None, "Order Total",
                              "Order total", amt, raw=raw_ln)
                res.rows.append(r)
                expect = (order_net or Decimal("0")) + order_extra
                if abs(expect - amt) > TOL:
                    r.flag(f"Net pay plus this order's earnings/"
                           f"reimbursements sum to {expect:,.2f} but the "
                           f"printed ORDER TOTAL is {amt:,.2f}.")
            continue

        if re.match(r"^Driver Deductions/Earnings$", ln):
            close_order()
            section = "driver_deductions"
            continue
        if re.match(r"^RECURRING DEDUCTIONS/EARNINGS$", ln, re.I):
            section = "recurring"
            continue
        if ln == "DEDUCTIONS":
            section = "deductions"
            continue
        if ln == "REIMBURSEMENTS":
            section = "reimbursements"
            continue
        if re.match(r"^SUBTOTAL FOR DRIVER", ln):
            section = "driver_subtotal"
            continue
        if ln.startswith("PAY SUMMARY"):
            section = "pay_summary"
            ln = ln[len("PAY SUMMARY"):].strip()
            if not ln:
                continue
        if ln == "DISPATCH SUMMARY":
            section = "dispatch"
            continue
        if ln == "ESCROW ACTIVITY":
            section = "escrow"
            continue
        if ln == "YTD SUMMARY":
            section = "ytd"
            continue

        if section == "order":
            m = SHELTON_LOAD_ROW.match(ln)
            if m:
                order_net = _dec(m.group("net"))
                route = m.group("route").strip()
                # Gross Pay is printed on the same line but is not part of
                # the arithmetic chain (Net Pay = Gross Pay x driver rate%,
                # already accounted for by Net Pay itself) -- captured
                # here purely so the figure is never silently dropped.
                res.rows.append(SettleRow(
                    "Load", cur_order, route, "Gross Pay", "Gross pay",
                    _dec(m.group("gross")), raw=raw_ln))
                r = SettleRow("Load", cur_order, route,
                              "Net Pay", "Net pay", order_net, raw=raw_ln)
                res.rows.append(r)
                continue
            m = SHELTON_TYPED_ROW.match(ln)
            if m:
                amt = _shelton_clean_amt(m.group("amt"))
                if amt is not None:
                    r = SettleRow("Load", cur_order, None, m.group("type"),
                                  m.group("desc").strip(), amt, raw=raw_ln)
                    res.rows.append(r)
                    order_extra += amt
                    if m.group("type") == "Earning":
                        earning_rows.append(r)
                    elif m.group("type") == "Reimbursement":
                        reimb_order_rows.append(r)
                    continue

        elif section == "driver_deductions":
            m = SHELTON_TYPED_ROW.match(ln)
            if m:
                amt = _shelton_clean_amt(m.group("amt"))
                if amt is not None:
                    r = SettleRow("Driver Deductions/Earnings", None, None,
                                  m.group("type"), m.group("desc").strip(),
                                  amt, raw=raw_ln)
                    res.rows.append(r)
                    driver_deduct_rows.append(r)
                    continue
            amt = _shelton_clean_amt(ln)
            if amt is not None:
                dd_subtotal = amt
                continue

        elif section == "recurring":
            m = SHELTON_RECURRING_ROW.match(ln)
            if m:
                amt = _shelton_clean_amt(m.group("amt"))
                if amt is not None:
                    r = SettleRow("Recurring Deductions/Earnings", None,
                                  None, m.group("type"),
                                  m.group("desc").strip(), amt, raw=raw_ln)
                    res.rows.append(r)
                    recurring_rows.append(r)
                    continue
            amt = _shelton_clean_amt(ln)
            if amt is not None:
                res.rows.append(SettleRow(
                    "Recurring Deductions/Earnings", None, None, "Subtotal",
                    "Section subtotal", amt, raw=raw_ln))
                recurring_subtotal = amt
                continue

        elif section == "deductions":
            m = SHELTON_PLAIN_ROW.match(ln)
            if m:
                amt = _shelton_clean_amt(m.group("amt"))
                if amt is not None:
                    r = SettleRow("Deductions", None, None, "Deduction",
                                  m.group("desc").strip(), amt, raw=raw_ln)
                    res.rows.append(r)
                    deduction_rows.append(r)
                    continue
            amt = _shelton_clean_amt(ln)
            if amt is not None:
                res.rows.append(SettleRow(
                    "Deductions", None, None, "Subtotal",
                    "Section subtotal", amt, raw=raw_ln))
                deductions_subtotal = amt
                continue

        elif section == "reimbursements":
            m = SHELTON_PLAIN_ROW.match(ln)
            if m:
                amt = _shelton_clean_amt(m.group("amt"))
                if amt is not None:
                    r = SettleRow("Reimbursements", None, None,
                                  "Reimbursement", m.group("desc").strip(),
                                  amt, raw=raw_ln)
                    res.rows.append(r)
                    reimbursement_rows.append(r)
                    continue
            amt = _shelton_clean_amt(ln)
            if amt is not None:
                res.rows.append(SettleRow(
                    "Reimbursements", None, None, "Subtotal",
                    "Section subtotal", amt, raw=raw_ln))
                reimbursements_subtotal = amt
                continue

        elif section == "escrow":
            m = SHELTON_ACCOUNT_HDR.match(ln)
            if m:
                name = m.group("name").strip()
                escrow_accounts.append({"name": name, "previous": None,
                                        "deposits": None,
                                        "withdrawals": None, "new": None})
                res.rows.append(SettleRow("Escrow Activity", None, name,
                                          "Account", name, Decimal("0.00"),
                                          raw=raw_ln))
                continue

        elif section == "dispatch":
            m = SHELTON_LABELED.match(ln)
            if m:
                s = re.sub(r"[_\s]", "", m.group("amt"))
                res.rows.append(SettleRow(
                    "Dispatch Summary", None, None, "Info",
                    f"{m.group('label').strip()}: {s}", Decimal("0.00"),
                    raw=raw_ln))
                continue

        m = SHELTON_LABELED.match(ln)
        if m:
            amt = _shelton_clean_amt(m.group("amt"))
            if amt is not None:
                label = m.group("label").strip()
                row_section = {
                    "driver_subtotal": "Driver Subtotal",
                    "pay_summary": "Pay Summary",
                    "escrow": "Escrow Activity",
                    "ytd": "YTD Summary",
                }.get(section, "Summary")
                r = SettleRow(row_section, None, None, "Total", label, amt,
                              raw=raw_ln)
                res.rows.append(r)
                if section == "driver_subtotal":
                    driver_subtotal_vals[label.upper()] = amt
                elif section == "pay_summary":
                    pay_summary_vals[label.upper()] = amt
                elif section == "escrow" and escrow_accounts:
                    acct = escrow_accounts[-1]
                    key = label.upper()
                    if "PREVIOUS" in key:
                        acct["previous"] = amt
                    elif "WITHDRAWAL" in key:
                        acct["withdrawals"] = amt
                    elif "DEPOSIT" in key:
                        acct["deposits"] = amt
                    elif "NEW BALANCE" in key:
                        acct["new"] = amt
                continue

    close_order()

    # ---------------------------------------------------------- proof -----
    ok = True

    if any(r.status != "OK" for r in res.rows
           if r.section == "Load" and r.rtype == "Order Total"):
        ok = False
        notes.append("One or more ORDER TOTAL lines do not match Net Pay "
                     "plus that order's earnings/reimbursements.")

    dd_sum = sum(r.amount for r in driver_deduct_rows)
    if dd_subtotal is not None and abs(dd_sum - dd_subtotal) > TOL:
        ok = False
        notes.append(f"Driver-level fuel/DEF deductions sum to "
                     f"{dd_sum:,.2f} but the printed subtotal is "
                     f"{dd_subtotal:,.2f}.")

    recurring_sum_early = sum(r.amount for r in recurring_rows)
    if recurring_subtotal is not None \
            and abs(recurring_sum_early - recurring_subtotal) > TOL:
        ok = False
        notes.append(f"Recurring deduction/earning rows sum to "
                     f"{recurring_sum_early:,.2f} but the printed section "
                     f"subtotal is {recurring_subtotal:,.2f}.")
    itemized_sum_early = sum(r.amount for r in deduction_rows)
    if deductions_subtotal is not None \
            and abs(itemized_sum_early - deductions_subtotal) > TOL:
        ok = False
        notes.append(f"Itemized DEDUCTIONS rows sum to "
                     f"{itemized_sum_early:,.2f} but the printed section "
                     f"subtotal is {deductions_subtotal:,.2f}.")
    reimb_page3_sum_early = sum(r.amount for r in reimbursement_rows)
    if reimbursements_subtotal is not None \
            and abs(reimb_page3_sum_early - reimbursements_subtotal) > TOL:
        ok = False
        notes.append(f"REIMBURSEMENTS rows sum to "
                     f"{reimb_page3_sum_early:,.2f} but the printed section "
                     f"subtotal is {reimbursements_subtotal:,.2f}.")

    order_pay1 = driver_subtotal_vals.get("ORDER PAY")
    driver_ded1 = driver_subtotal_vals.get("DRIVER DEDUCTIONS/EARNINGS")
    driver_net = driver_subtotal_vals.get("DRIVER NET PAY")
    if order_pay1 is not None:
        s = sum(order_totals)
        if abs(s - order_pay1) > TOL:
            ok = False
            notes.append(f"The {len(order_totals)} ORDER TOTAL values sum "
                         f"to {s:,.2f} but the printed ORDER PAY (driver "
                         f"subtotal) is {order_pay1:,.2f}.")
    if order_pay1 is not None and driver_ded1 is not None \
            and driver_net is not None:
        expect = order_pay1 + driver_ded1
        if abs(expect - driver_net) > TOL:
            ok = False
            notes.append(f"ORDER PAY {order_pay1:,.2f} plus DRIVER "
                         f"DEDUCTIONS/EARNINGS {driver_ded1:,.2f} gives "
                         f"{expect:,.2f}, but the printed DRIVER NET PAY is "
                         f"{driver_net:,.2f}.")
    if driver_ded1 is not None and dd_subtotal is not None \
            and abs(driver_ded1 - dd_subtotal) > TOL:
        ok = False
        notes.append(f"DRIVER DEDUCTIONS/EARNINGS ({driver_ded1:,.2f}) does "
                     f"not match the driver-level deduction subtotal "
                     f"({dd_subtotal:,.2f}).")

    order_pay2 = pay_summary_vals.get("ORDER PAY")
    other_earnings = pay_summary_vals.get("OTHER EARNINGS")
    gross = pay_summary_vals.get("TOTAL GROSS EARNINGS")
    deductions2 = pay_summary_vals.get("DEDUCTIONS")
    reimb2 = pay_summary_vals.get("EXPENSE REIMBURSEMENTS")
    net_pay = pay_summary_vals.get("NET PAY")

    if order_pay2 is not None and order_nets:
        s = sum(order_nets)
        if abs(s - order_pay2) > TOL:
            ok = False
            notes.append(f"The {len(order_nets)} loads' Net Pay values sum "
                         f"to {s:,.2f} but the PAY SUMMARY ORDER PAY is "
                         f"{order_pay2:,.2f}.")
    if other_earnings is not None:
        s = sum(r.amount for r in earning_rows)
        if abs(s - other_earnings) > TOL:
            ok = False
            notes.append(f"The per-order Earning rows sum to {s:,.2f} but "
                         f"the printed OTHER EARNINGS is "
                         f"{other_earnings:,.2f}.")
    if order_pay2 is not None and other_earnings is not None \
            and gross is not None:
        expect = order_pay2 + other_earnings
        if abs(expect - gross) > TOL:
            ok = False
            notes.append(f"ORDER PAY {order_pay2:,.2f} plus OTHER EARNINGS "
                         f"{other_earnings:,.2f} gives {expect:,.2f}, but "
                         f"the printed TOTAL GROSS EARNINGS is "
                         f"{gross:,.2f}.")
    recurring_sum = sum(r.amount for r in recurring_rows)
    itemized_sum = sum(r.amount for r in deduction_rows)
    if deductions2 is not None:
        expect = dd_sum + recurring_sum + itemized_sum
        if abs(expect - deductions2) > TOL:
            ok = False
            notes.append(f"Driver-level deductions ({dd_sum:,.2f}) plus "
                         f"recurring ({recurring_sum:,.2f}) plus itemized "
                         f"({itemized_sum:,.2f}) gives {expect:,.2f}, but "
                         f"the printed DEDUCTIONS is {deductions2:,.2f}.")
    reimb_order_sum = sum(r.amount for r in reimb_order_rows)
    reimb_page3_sum = sum(r.amount for r in reimbursement_rows)
    if reimb2 is not None:
        expect = reimb_order_sum + reimb_page3_sum
        if abs(expect - reimb2) > TOL:
            ok = False
            notes.append(f"Per-order reimbursements ({reimb_order_sum:,.2f}) "
                         f"plus page-3 reimbursements "
                         f"({reimb_page3_sum:,.2f}) gives {expect:,.2f}, but "
                         f"the printed EXPENSE REIMBURSEMENTS is "
                         f"{reimb2:,.2f}.")
    if gross is not None and deductions2 is not None and reimb2 is not None \
            and net_pay is not None:
        expect = gross + deductions2 + reimb2
        if abs(expect - net_pay) > TOL:
            ok = False
            notes.append(f"TOTAL GROSS EARNINGS {gross:,.2f} plus "
                         f"DEDUCTIONS {deductions2:,.2f} plus EXPENSE "
                         f"REIMBURSEMENTS {reimb2:,.2f} gives {expect:,.2f}, "
                         f"but the printed NET PAY is {net_pay:,.2f}.")
    if net_pay is None:
        ok = False
        notes.append("No final NET PAY line was found.")

    for acct in escrow_accounts:
        if acct["new"] is not None and acct["previous"] is not None:
            dep = acct["deposits"] or Decimal("0")
            # WITHDRAWALS is printed already signed negative
            # ("WITHDRAWALS: -$457.74"), so it is added, not subtracted.
            wd = acct["withdrawals"] or Decimal("0")
            expect = acct["previous"] + dep + wd
            if abs(expect - acct["new"]) > TOL:
                ok = False
                notes.append(
                    f"Escrow account '{acct['name']}': previous balance "
                    f"{acct['previous']:,.2f} + deposits {dep:,.2f} + "
                    f"withdrawals {wd:,.2f} gives {expect:,.2f}, but the "
                    f"printed NEW BALANCE is {acct['new']:,.2f}.")

    res.recon_passed = ok
    if ok:
        res.recon_detail = (
            f"Every ORDER TOTAL matches Net Pay plus that order's "
            f"earnings/reimbursements, the driver-level, recurring and "
            f"itemized deduction subtotals sum to the printed DEDUCTIONS "
            f"({deductions2:,.2f}), the reimbursement rows sum to the "
            f"printed EXPENSE REIMBURSEMENTS ({reimb2:,.2f}), and TOTAL "
            f"GROSS EARNINGS minus DEDUCTIONS plus EXPENSE REIMBURSEMENTS "
            f"equals the printed NET PAY ({net_pay:,.2f})."
            if net_pay is not None else
            "All available printed subtotals match the extracted rows.")
    else:
        res.recon_detail = " ".join(notes)
    return res
