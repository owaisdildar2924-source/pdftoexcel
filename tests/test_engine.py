"""Tests for the defects listed in the build brief. Every one of these was
a real bug that reached a user in a previous build."""
import os
import re
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bankconv.core import (Word, group_lines, parse_date_token, parse_money,
                           YEAR_RE)
from bankconv.parse import RowScan, StatementParser, find_period
from bankconv.profiles import CATCH_ALL, Profile
from bankconv.reconcile import reconcile_block
from bankconv.core import AccountBlock, Txn


def W(text, x0, top=0.0, w=None):
    w = w if w is not None else 6.0 * len(text)
    return Word(text, x0, x0 + w, top, top + 10)


def make_line_words(tokens, top):
    x = 30.0
    out = []
    for t in tokens:
        out.append(W(t, x, top))
        x += 6.0 * len(t) + 6
    return out


# ---------------------------------------------------------------- money ----

def test_trailing_minus():
    m = parse_money("1,003.00-")
    assert m and m.explicit_sign == -1 and m.value == Decimal("1003.00")


def test_leading_dollar_minus():
    m = parse_money("-$30.00")
    assert m and m.explicit_sign == -1


def test_plain_positive_has_no_sign():
    m = parse_money("$466.53")
    assert m and m.explicit_sign is None


# ---------------------------------------------------------------- dates ----

def test_year_pattern_never_matches_inside_money():
    # \b(20\d\d)\b once matched the 2000 in 2,000.00 and dated a whole
    # file to the year 2000
    assert YEAR_RE.search("2,000.00") is None
    assert YEAR_RE.search("balance 2022 total") is not None


def test_yearless_date_formats():
    for tok in ("01/31", "1-31", "01-31"):
        d = parse_date_token(tok)
        assert d is not None and d.year is None, tok


def test_year_rollover():
    # Dec->Jan statement: 01/03 resolves to the NEXT year
    period = (date(2021, 12, 14), date(2022, 1, 13))
    from bankconv.core import resolve_year, DateToken
    d = resolve_year(DateToken(1, 3, None, "01/03"), period)
    assert d == date(2022, 1, 3)
    d = resolve_year(DateToken(12, 17, None, "12/17"), period)
    assert d == date(2021, 12, 17)


def test_period_with_glued_through():
    p = find_period("January 01, 2022throughJanuary 31, 2022")
    assert p == (date(2022, 1, 1), date(2022, 1, 31))


# ------------------------------------------------------------- structure ----

def _parse(pages, profile=None):
    prof = profile or CATCH_ALL
    texts = []
    for words in pages:
        lines = group_lines(words, 1)
        texts.append("\n".join(ln.text for ln in lines))
    sp = StatementParser(pages, prof, "\n".join(texts))
    return sp.parse(), sp


def test_daily_balance_table_rejected():
    # a date and an amount on every row, no description -> not transactions
    rows = [make_line_words(["08/01", "1,369.87"], 100),
            make_line_words(["08/02", "64.95"], 115)]
    page = [w for r in rows for w in r]
    blocks, _ = _parse([page])
    assert sum(len(b.txns) for b in blocks) == 0


def test_wrapped_amount_on_next_line():
    # amount on the continuation line directly below the dated line
    r1 = make_line_words(["03-03", "POS", "Debit", "Grocery", "Store"], 100)
    r2 = make_line_words(["Decatur", "3.35-"], 115)
    blocks, _ = _parse([r1 + r2])
    txns = [t for b in blocks for t in b.txns]
    assert len(txns) == 1
    assert txns[0].amount == Decimal("-3.35")


def test_dated_line_without_amount_is_dropped():
    r1 = make_line_words(["October", "31,", "2024", "Page", "1", "of", "5"], 100)
    r2 = make_line_words(["Questions?", "call", "us"], 115)
    blocks, _ = _parse([r1 + r2])
    assert sum(len(b.txns) for b in blocks) == 0


def test_balance_forward_starts_new_account():
    r1 = make_line_words(["01/01", "ID", "0001", "SAVINGS", "Balance",
                          "Forward", "500.00"], 100)
    r2 = make_line_words(["01/05", "Withdrawal", "Transfer", "100.00-",
                          "400.00"], 115)
    r3 = make_line_words(["01/01", "ID", "0050", "CHECKING", "Balance",
                          "Forward", "50.00"], 130)
    prof = Profile(key="t", display="t", institution="t", fingerprints=[],
                   has_running_balance=True)
    blocks, _ = _parse([r1 + r2 + r3], prof)
    assert len(blocks) == 2
    assert blocks[0].beginning == Decimal("500.00")
    assert blocks[1].beginning == Decimal("50.00")


# ---------------------------------------------------------- reconcile ------

def _txn(amount, balance=None):
    return Txn(date=None, date_raw="", description="x",
               amount=Decimal(str(amount)),
               balance=Decimal(str(balance)) if balance is not None else None,
               page=1)


def test_sparse_balance_group_check():
    # five transactions share one printed balance: check the group,
    # not each row (a previous build ambered nearly every row)
    b = AccountBlock(name="", beginning=Decimal("100"))
    b.txns = [_txn(10), _txn(20), _txn(-5, balance=125)]
    reconcile_block(b)
    assert all(t.status == "OK" for t in b.txns)


def test_single_row_mismatch_names_figures():
    b = AccountBlock(name="", beginning=Decimal("100"))
    b.txns = [_txn(16.34, balance=53.66)]   # printed balance moved by -46.34
    reconcile_block(b)
    t = b.txns[0]
    assert t.status == "NEEDS_REVIEW"
    assert "16.34" in t.review_note and "-46.34" in t.review_note


def test_file_level_reconciliation_does_not_flag_rows():
    b = AccountBlock(name="", beginning=Decimal("100"),
                     ending=Decimal("999"))
    b.txns = [_txn(10), _txn(20)]
    reconcile_block(b)
    assert b.recon_passed is False
    assert all(t.status == "OK" for t in b.txns)   # file problem, not rows


def test_never_overwrites_amounts():
    # the engine reports mismatches; it must never change a figure
    b = AccountBlock(name="", beginning=Decimal("100"))
    b.txns = [_txn(16.34, balance=53.66)]
    reconcile_block(b)
    assert b.txns[0].amount == Decimal("16.34")


# ------------------------------------------------------------- network -----

def test_no_network_libraries_imported():
    """Rule: never make a network call. No network library may be imported
    anywhere in shipped code."""
    banned = re.compile(
        r"^\s*(import|from)\s+(requests|urllib|http\b|httpx|aiohttp|socket"
        r"|ftplib|smtplib|telnetlib|websocket)", re.M)
    src_dir = os.path.join(os.path.dirname(__file__), "..", "bankconv")
    offenders = []
    for fn in os.listdir(src_dir):
        if fn.endswith(".py"):
            with open(os.path.join(src_dir, fn), encoding="utf-8") as fh:
                if banned.search(fh.read()):
                    offenders.append(fn)
    for fn in ("app.py",):
        with open(os.path.join(src_dir, "..", fn), encoding="utf-8") as fh:
            if banned.search(fh.read()):
                offenders.append(fn)
    assert not offenders, f"network imports found in: {offenders}"


# ------------------------------------------------------ completeness -------

SETTLEMENT_FIXTURE = """Adriana Duncan (3/7/2025)
COD Job #
Sarah Shewan $3,305.71 1551966-CV $5,269.00 Total Cost
-$499.00 Shuttle PU
-$82.77 Insurance
$4,687.23
Total Jobs: $4,687.23 $153.00 1551966-CV
3% CC Fee Job #1551966-CV -$66.28
TOTAL PAY TO DRIVER $4,620.95
**ESTIMATED REPAIRS FOR ACCIDENT**
Settlement Week $3,500.00
1/10/2025 -$500.00
Balance (estimated) $3,000.00
**MISSING DELIVERY DOCS FOR JOB(S) BELOW**
Settlement Week Job #
3/7/2025 1569603-CV del 3/2 paid
"""


def test_settlement_captures_every_printed_money_value():
    """Nothing printed on the page may be silently dropped.

    A previous version stopped at TOTAL PAY TO DRIVER and lost the
    accident-repair and missing-docs tables, and the Comdata breakdown
    column, without saying so.
    """
    from bankconv.settlement import parse_colonial
    res = parse_colonial(SETTLEMENT_FIXTURE)
    money = re.compile(r"-?\$\d{1,3}(?:,\d{3})*\.\d{2}")
    printed = {abs(Decimal(m.replace("$", "").replace(",", "")))
               for m in money.findall(SETTLEMENT_FIXTURE)}
    captured = {abs(r.amount) for r in res.rows}
    captured |= {abs(Decimal(m.replace("$", "").replace(",", "")))
                 for m in money.findall(" ".join(r.description for r in res.rows))}
    assert not (printed - captured), f"dropped values: {sorted(printed - captured)}"


def test_trailer_tables_are_separate_from_the_arithmetic():
    """The accident table must be captured, but must NOT be counted as
    settlement adjustments or the driver total would be wrong."""
    from bankconv.settlement import parse_colonial
    res = parse_colonial(SETTLEMENT_FIXTURE)
    sections = {r.section for r in res.rows}
    assert "Estimated Repairs For Accident" in sections
    assert "Missing Delivery Docs For Job(S) Below" in sections
    adjustments = sum(r.amount for r in res.rows
                      if r.section == "Summary" and r.rtype == "Adjustment")
    assert adjustments == Decimal("-66.28")
    assert res.recon_passed is True


# ------------------------------------------------------- completeness ------

def test_hundred_plus_pages_all_read():
    """Some real bundles run past 100 pages (Synchrony statements combine
    several months into one file). The engine must read every page, not
    just the first few — build 130 one-transaction pages and confirm all
    130 rows are captured, including the transaction on the very last
    page, which a page-count cap or an early stop would silently drop."""
    profile = Profile(key="t", display="t", institution="t", fingerprints=[])
    pages = []
    for i in range(130):
        day = (i % 28) + 1
        # explicit trailing-minus sign so the row's direction never depends
        # on wording — only completeness is under test here
        line = make_line_words(
            [f"01/{day:02d}", "Misc", "Item", f"{i + 1}.00-"], 100)
        pages.append(line)
    blocks, sp = _parse(pages, profile)
    txns = [t for b in blocks for t in b.txns]
    assert sp.total_pages == 130
    assert len(txns) == 130, f"expected 130 rows, got {len(txns)}"
    last_page_txns = [t for t in txns if t.page == 130]
    assert len(last_page_txns) == 1, "the last page's row was not read"
    assert last_page_txns[0].amount == Decimal("-130.00")
    total = sum(t.amount for t in txns)
    assert total == Decimal(str(-sum(range(1, 131))))


def test_stop_marker_mid_document_is_flagged_not_hidden():
    """A stop marker (back-matter boilerplate) is meant to end a document
    that has finished, not to silently truncate one that has more pages of
    real transactions after it. unread_tail_has_money() must catch this."""
    profile = Profile(key="t", display="t", institution="t", fingerprints=[],
                      stop_markers=[r"IMPORTANT INFORMATION"])
    pages = [
        make_line_words(["01/01", "Opening", "Item", "10.00-"], 100),
        [W("IMPORTANT INFORMATION ABOUT YOUR ACCOUNT", 30, 100)],
        make_line_words(["01/15", "Later", "Item", "25.00-"], 100),
    ]
    blocks, sp = _parse(pages, profile)
    assert sp.stopped is True
    assert sp.unread_tail_has_money() is True


def test_unread_tail_ignores_dollar_figures_with_no_date_on_the_line():
    """Legal boilerplate that merely names a dollar amount ('...filing fees
    up to $700.00') is not a dropped transaction row. A real Wells Fargo
    file was marked permanently unproven by this over-firing even after
    every real row had been read — the check must require a date AND a
    money figure on the SAME line, the same shape the engine itself needs
    to recognise an actual row, not just the presence of a dollar sign."""
    profile = Profile(key="t", display="t", institution="t", fingerprints=[],
                      stop_markers=[r"ARBITRATION"])
    pages = [
        make_line_words(["01/01", "Opening", "Item", "10.00-"], 100),
        [W("ARBITRATION reimburse filing fees up to $700.00 no date here", 30, 100)],
    ]
    blocks, sp = _parse(pages, profile)
    assert sp.stopped is True
    assert sp.unread_tail_has_money() is False


def test_resume_marker_waives_a_stop_that_precedes_real_content():
    """Some issuers interleave a disclosure page BEFORE a 'continued'
    transaction table instead of after it (Wells Fargo credit card: page 1
    references 'see reverse side', page 2 is the real disclosure page,
    page 3 resumes with 'Transactions (continued from previous page)' and
    a real dated row). Without resume_markers this would stop for good on
    page 2 and silently drop page 3's row; with it, the stop is waived and
    the real row is still captured."""
    profile = Profile(key="t", display="t", institution="t", fingerprints=[],
                      stop_markers=[r"IMPORTANT INFORMATION"],
                      resume_markers=[r"continued from previous page"])
    pages = [
        make_line_words(["01/01", "Opening", "Item", "10.00-"], 100),
        [W("IMPORTANT INFORMATION ABOUT YOUR ACCOUNT disclosures here", 30, 100)],
        [W("Transactions (continued from previous page)", 30, 80)]
        + make_line_words(["01/15", "Later", "Item", "25.00-"], 100),
    ]
    blocks, sp = _parse(pages, profile)
    txns = [t for b in blocks for t in b.txns]
    assert len(txns) == 2, "the row after the waived stop was dropped"
    assert sp.unread_tail_has_money() is False


# ------------------------------------------------------------ skew ---------

def test_straight_page_not_sheared():
    # a page of scattered text must NOT acquire a phantom skew (this
    # silently interleaved rows of a straight statement)
    import random
    random.seed(7)
    words = []
    for row in range(30):
        for _ in range(4):
            x = random.uniform(30, 500)
            words.append(W("token", x, 40 + row * 14))
    lines = group_lines(words, 1)
    assert len(lines) == 30


# ---------------------------------------------------- new-format defects ---
# Each of these reproduces a real bug found while building the 2024/2025
# native-PDF formats (Bank of America, Credit Union of Atlanta, Amazon
# Business Amex, Credit One Bank, Truist's new layout, Synchrony private
# label cards, Shelton Trucking settlements) so it can never come back.

def test_leading_reference_number_before_date_is_kept_as_description():
    # Credit One Bank: 'F638800EF000FR063 03/03 03/03 ... -7.07' — a
    # reference number sits before the date. The old code only ever
    # checked ws[0] for a date, so a leading non-date token silently
    # dropped the entire row (amount and all).
    r = make_line_words(
        ["F638800EF000FR063", "03/03", "03/03", "CREDIT", "REWARD", "-7.07"],
        100)
    blocks, _ = _parse([r])
    txns = [t for b in blocks for t in b.txns]
    assert len(txns) == 1
    assert txns[0].amount == Decimal("-7.07")
    assert "F638800EF000FR063" in txns[0].description


def test_date_token_tolerates_trailing_footnote_marker():
    # Amex posting-date marker: '12/16/25*' means "posting date" per the
    # issuer's own key. A strict token match failed on the trailing '*'
    # and silently dropped the whole payment row, amount included.
    d = parse_date_token("12/16/25*")
    assert d is not None and d.month == 12 and d.day == 16 and d.year == 2025


def test_section_overrides_row_words_when_enabled():
    # Bank of America: a refund sitting in the credit-only 'Deposits'
    # section ('MOBILE PURCHASE ... $21.77') was flipped to a debit by the
    # generic '\bpurchase\b' row-word rule. With the override on, the
    # printed section — a hard proof line on this issuer — wins.
    prof = Profile(key="t", display="t", institution="t", fingerprints=[],
                   sign_convention="sections",
                   credit_sections=[r"^Deposits$"],
                   debit_sections=[r"^Withdrawals$"],
                   section_overrides_row_words=True)
    r1 = make_line_words(["Deposits"], 90)
    r2 = make_line_words(["01/05", "MOBILE", "PURCHASE", "REFUND", "21.77"], 105)
    blocks, _ = _parse([r1 + r2], prof)
    txns = [t for b in blocks for t in b.txns]
    assert len(txns) == 1
    assert txns[0].amount == Decimal("21.77")
    assert txns[0].sign_source == "section"


def test_new_block_only_appends_when_previous_block_has_content():
    # Truist's two-account consolidated statement: an account_markers
    # heading and a balance-forward row fired back-to-back for the same
    # logical account, and the old 'already started' check keyed off
    # b.name truthiness alone, creating a spurious empty duplicate block
    # that broke a file that used to reconcile as one block.
    prof = Profile(key="t", display="t", institution="t", fingerprints=[],
                   account_markers=[r"^ACCOUNT\s+(\d+)$"])
    r1 = make_line_words(["ACCOUNT", "12345"], 80)
    r2 = make_line_words(["01/01", "Beginning", "Balance", "100.00"], 95)
    r3 = make_line_words(["01/05", "Withdrawal", "Transfer", "40.00-"], 110)
    blocks, _ = _parse([r1 + r2 + r3], prof)
    assert len(blocks) == 1
    assert blocks[0].beginning == Decimal("100.00")
    assert len(blocks[0].txns) == 1


def test_section_totals_are_scoped_per_account_block():
    # A consolidated statement prints its own 'Deposits, credits and
    # interest' total per account (checking, then savings). Pooling every
    # block's rows together made a correct total look wrong by exactly
    # the other account's sum.
    from bankconv.reconcile import check_section_totals
    b1 = AccountBlock(name="Checking")
    b1.txns = [Txn(date=None, date_raw="", description="d1",
                   amount=Decimal("100.00"), balance=None, page=1,
                   section="Deposits")]
    b2 = AccountBlock(name="Savings")
    b2.txns = [Txn(date=None, date_raw="", description="d2",
                   amount=Decimal("50.00"), balance=None, page=1,
                   section="Deposits")]
    totals = [(b1, "deposits", Decimal("100.00")),
             (b2, "deposits", Decimal("50.00"))]
    notes = check_section_totals([b1, b2], totals)
    assert notes == []


def test_synchrony_payments_heading_not_confused_with_summary_box():
    # Synchrony private-label cards (Amazon Store Card, Sam's Club
    # Mastercard, Techron Advantage) print 'Payments - 400.00 Available
    # Credit $1,153' with NO '$' sign in the page-1 Account Summary box,
    # right above a coupon-stub line ('05/14/24 New Balance $5,746.15')
    # that is itself a date + description + money row. A loose '^Payments'
    # section pattern matched the summary line first, going live a whole
    # page early and turning the coupon restatement into a phantom
    # transaction. Requiring '$' and a full-line anchor must reject it.
    pat = r"^Payments\s+-?\$[\d,]+\.\d{2}\s*$"
    assert re.search(pat, "Payments - 400.00 Available Credit $1,153") is None
    assert re.search(pat, "Payments -$142.65") is not None


def test_shelton_settlement_reconciles_and_captures_every_dollar_figure():
    """Compact synthetic fixture reproducing the Shelton Trucking Owner
    Operator Settlement Summary shape: per-load tables -> driver-level
    fuel/DEF deductions -> a recurring lease-escrow deduction -> itemized
    deductions/reimbursements -> a PAY SUMMARY reconciling to NET PAY. The
    PDF underlines the last row before every subtotal, which the text
    layer renders as '_' interleaved with the digits — reproduced here on
    the driver-deduction subtotal and the final NET PAY line."""
    from bankconv.settlement import parse_shelton
    fixture = """SETTLEMENT 0000001
CITY A ST CITY B ST Loaded 100.0 DS1 01/01/25 01/01/25 $200.00 68.00% $136.00
Percentage
Order Deductions/Earnings
Type Description Memo Date Unit Rate
Reimbursement Fuel Surcharge Reimbursement 01/01/25 1.00 20.000 $20.00
__________
ORDER TOTAL $156.00
Driver Deductions/Earnings
Type Description Memo Date Unit Rate
Deduction Fuel: DS1 CITY B ST 0000001 01/01/25 1.00 30.000___-_$_3_0._0_0_
-$30.00
SUBTOTAL FOR DRIVER TEST
ORDER PAY: $156.00
DRIVER DEDUCTIONS/EARNINGS: -$30.00
DRIVER NET PAY: $126.00
PAY SUMMARY ORDER PAY: $136.00
OTHER EARNINGS: $0.00
TOTAL GROSS EARNINGS: $136.00
DEDUCTIONS: -$30.00
EXPENSE REIMBURSEMENTS: $20.00
NET PAY: ____ $__ 1__ 2__ 6__ .__ 0__ 0__
"""
    res = parse_shelton(fixture)
    assert res.recon_passed is True, res.recon_detail
    money = re.compile(r"-?\$\d{1,3}(?:,\d{3})*\.\d{2}")
    printed = {abs(Decimal(m.replace("_", "").replace("$", "").replace(",", "")))
               for m in money.findall(fixture.replace("_", ""))}
    captured = {abs(r.amount) for r in res.rows}
    assert not (printed - captured), f"dropped values: {sorted(printed - captured)}"


# ------------------------------------------------ August 2026 new formats --
# Each of these reproduces a real defect found while adding three more real
# statement formats (Regions Bank business checking, Chase Marriott Bonvoy
# credit card, Citi /AAdvantage Executive World Mastercard).

def _profile(key):
    from bankconv.profiles import PROFILES
    return next(p for p in PROFILES if p.key == key)


def test_bare_decimal_amount_without_leading_zero_is_parsed():
    # Chase Marriott Bonvoy prints at least one sub-dollar amount as '.68'
    # rather than '0.68' ('05/10 WALMART.COM 800-925-6278 AR .68'). This
    # used to fail parse_money silently -- no flag, no warning, the row
    # just never became a transaction -- and was only caught because
    # reconciliation was off by exactly that amount on a real statement.
    m = parse_money(".68")
    assert m is not None and m.value == Decimal("0.68")


def test_regions_daily_balance_summary_not_read_as_transactions():
    # 'DAILY BALANCE SUMMARY' prints three columns of date+balance pairs
    # per line ('04/01 28,900.28 04/11 32,483.77 04/22 31,299.50') strictly
    # after the real Deposits/Withdrawals/Fees sections -- exactly the
    # date+money shape a row scan looks for. The stop_marker must exclude
    # it entirely rather than relying on the description-length filter.
    profile = _profile("regions_business_checking")
    pages = [
        make_line_words(["DEPOSITS", "&", "CREDITS"], 60)
        + make_line_words(["04/01", "Some", "Deposit", "2,315.24"], 80)
        + [W("WITHDRAWALS", 30, 100)]
        + make_line_words(["04/01", "Some", "Purchase", "10.00"], 120)
        + [W("DAILY BALANCE SUMMARY", 30, 140)]
        + make_line_words(["04/01", "28,900.28", "04/11", "32,483.77"], 160),
    ]
    blocks, sp = _parse(pages, profile)
    txns = [t for b in blocks for t in b.txns]
    assert len(txns) == 2, "the daily balance grid leaked in as transactions"


def test_citi_purchases_heading_glued_to_page_footer_code():
    # Every page prints a small print-imposition code ('033200') pinned to
    # the bottom margin. On months where the payments list is a different
    # length, that code's vertical position lands close enough to
    # 'Standard Purchases' to merge onto the same visual line
    # ('033200 Standard Purchases'), which a full '^...$' anchor would
    # miss entirely -- leaving every purchase mis-signed as a payment
    # credit instead of a charge. Reproduced here as an actual merged Line.
    profile = _profile("citi_aadvantage_mastercard")
    pages = [
        [W("Payments, Credits and Adjustments", 30, 60)]
        + make_line_words(["03/17", "ONLINE PAYMENT, THANK YOU", "-$124.10"], 80)
        + [W("033200 Standard Purchases", 30, 100)]
        + make_line_words(["03/19", "03/19", "SOME MERCHANT", "$50.00"], 120),
    ]
    blocks, sp = _parse(pages, profile)
    txns = [t for b in blocks for t in b.txns]
    assert len(txns) == 2
    purchase = next(t for t in txns if "MERCHANT" in t.description)
    assert purchase.amount == Decimal("50.00"), (
        "purchase kept the payments section's credit sign instead of "
        "switching to a charge once 'Standard Purchases' was seen")


def test_chase_bonvoy_page1_graphic_produces_no_phantom_transactions():
    # Page 1 renders a circular 'at a glance' calendar/points graphic whose
    # overlapping text elements extract character-interleaved
    # ('M$i8nim7u9m. P1a7yment Due' for 'Minimum Payment Due $879.17') --
    # none of those fragments parse as a clean date or money token, but
    # require_section is the structural belt-and-braces guard: nothing
    # counts as a transaction until the real 'PAYMENTS AND OTHER CREDITS'
    # / 'PURCHASE' headings from the ACCOUNT ACTIVITY table are seen.
    profile = _profile("chase_marriott_bonvoy_credit")
    pages = [
        # page 1: garbled graphic fragment plus the (clean) balance box --
        # neither should produce a transaction before a real section header
        make_line_words(["06/22/26", "Total", "points", "transferred", "5,899"], 60)
        + make_line_words(["Previous", "Balance", "$239.51"], 80),
        # page 3 equivalent: the real transaction table
        [W("PAYMENTS AND OTHER CREDITS", 30, 60)]
        + make_line_words(["05/13", "Payment Thank You-Mobile", "-2,014.39"], 80)
        + [W("PURCHASE", 30, 100)]
        + make_line_words(["04/24", "COSTCO GAS #1333", "94.00"], 120),
    ]
    blocks, sp = _parse(pages, profile)
    txns = [t for b in blocks for t in b.txns]
    assert len(txns) == 2, (
        f"expected exactly the 2 real rows, got {len(txns)}: "
        f"{[(t.date, t.amount, t.description) for t in txns]}")
