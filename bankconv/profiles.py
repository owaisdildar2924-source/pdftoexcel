"""Thin per-issuer profiles.

A profile declares: how to recognise the issuer, which sign convention it
uses, whether it prints a running balance, and section vocabulary. Nothing
about layout — layout is the geometric engine's job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Profile:
    key: str
    display: str
    institution: str
    # recognition: (pattern, weight) pairs searched in first-two-pages text
    fingerprints: list[tuple[str, int]]
    is_credit_card: bool = False
    has_running_balance: bool = False
    balance_is_sparse: bool = False       # e.g. Wells Fargo ending daily balance
    sign_convention: str = "explicit"      # explicit | trailing | columns | sections
    # section headings that set direction for unsigned rows
    credit_sections: list[str] = field(default_factory=list)
    debit_sections: list[str] = field(default_factory=list)
    # regexes that end transaction parsing (back matter)
    stop_markers: list[str] = field(default_factory=list)
    # regexes that, if found on any LATER page, waive a stop_markers hit.
    # Almost every issuer prints boilerplate strictly after the real
    # content, so an unqualified stop is safe and simple. A few interleave
    # a disclosure page before a 'continued' transaction table instead —
    # stopping there for good would silently drop the real rows that
    # follow. Empty by default: this only changes behaviour for a profile
    # that explicitly opts in.
    resume_markers: list[str] = field(default_factory=list)
    # regexes that start a new account block (capture group 1 = name)
    account_markers: list[str] = field(default_factory=list)
    # labelled balances
    begin_labels: list[str] = field(default_factory=lambda: [
        r"beginning balance", r"previous balance", r"balance forward",
        r"last month'?s balance", r"opening balance", r"previous statement balance"])
    end_labels: list[str] = field(default_factory=lambda: [
        r"ending balance", r"new balance", r"closing balance",
        r"ending daily balance$"])
    # row words that decide direction regardless of section (checked first→last)
    row_credit_words: list[str] = field(default_factory=lambda: [
        r"\bdeposit\b", r"payment received", r"payment thank you",
        r"card payment\b", r"\bpayment from\b",
        r"\brefund\b", r"\breversal\b", r"\bcredit\b(?!.*card)",
        r"transfer from", r"\bdividend\b", r"cash reward", r"\brebate\b"])
    row_debit_words: list[str] = field(default_factory=lambda: [
        r"\bwithdrawal\b", r"\bpurchase\b", r"\bfee\b", r"\bcharge\b",
        r"transfer to", r"\bdebit\b", r"bill pay", r"\bpayment to\b",
        r"check\s+#?\d"])
    default_direction: int = 0            # 0 = none; -1 debit; +1 credit
    two_date_rows: bool = False           # credit cards printing trans+post date
    # Credit cards: does a printed sign equal the owed-balance delta?
    # +1: yes (Capital One, Chase: payments print negative and reduce debt)
    # -1: inverted (Chime prints from the cardholder's cash perspective:
    #     purchases negative, payments positive)
    cc_explicit_orientation: int = 1
    # Only treat dated rows as transactions inside a recognised section
    # (kills phantom rows built from summary boxes on page 1)
    require_section: bool = False
    # When a row sits under a printed section heading, trust that heading
    # over generic keyword guessing. Default False preserves the existing
    # order (row words first) that Chase's "Reversal: Monthly Service Fee"
    # fix depends on. Bank of America is the opposite case: its printed
    # section total is a hard proof line (e.g. 'Total deposits and other
    # additions'), so a refund oddly named 'MOBILE PURCHASE' that lands in
    # the Deposits section must stay a credit, not flip on the word
    # "purchase".
    section_overrides_row_words: bool = False


PROFILES: list[Profile] = [
    Profile(
        key="wells_fargo_checking",
        display="Wells Fargo Business Checking",
        institution="Wells Fargo",
        fingerprints=[(r"wells fargo", 40), (r"1-800-CALL-WELLS", 30),
                      (r"Transaction history", 20), (r"Ending daily\s*balance", 10)],
        has_running_balance=True,
        balance_is_sparse=True,
        sign_convention="columns",
        stop_markers=[r"Summary of checks written",
                      r"Worksheet to balance your account",
                      r"IMPORTANT ACCOUNT INFORMATION"],
    ),
    Profile(
        key="wells_fargo_credit",
        display="Wells Fargo Credit Card",
        institution="Wells Fargo",
        is_credit_card=True,
        fingerprints=[(r"WELLS FARGO CASH BACK|VISA SIGNATURE", 45),
                      (r"wells fargo", 30), (r"Minimum Payment Warning", 15)],
        sign_convention="explicit",
        # Page 1 prints a one-line pointer ('NOTICE: SEE REVERSE SIDE FOR
        # IMPORTANT INFORMATION ABOUT YOUR ACCOUNT') to the real disclosure
        # page — an unqualified match on the phrase fired right there,
        # stopping the whole document before the 'Transactions (continued
        # from previous page)' table on the next real page was ever read.
        # The negative lookbehind excludes only that pointer sentence; the
        # real standalone heading on the disclosure page itself ('—
        # IMPORTANT INFORMATION ABOUT YOUR ACCOUNT 1. What are your
        # billing rights?') is unaffected and still stops parsing there.
        stop_markers=[r"Interest Charge Calculation",
                      r"(?<!side for )Important Information About Your Account"],
        # this issuer sometimes interleaves the billing-rights disclosure
        # page BEFORE 'Transactions (continued from previous page)'
        # rather than after it; waive the stop when that phrase is found
        # on a later page instead of silently ending the document early
        resume_markers=[r"continued from previous page"],
        default_direction=-1,
    ),
    Profile(
        key="chase_checking",
        display="Chase Business Checking",
        institution="Chase",
        fingerprints=[(r"JPMorgan Chase Bank", 40), (r"chase\.com", 20),
                      (r"CHECKING SUMMARY", 25)],
        sign_convention="sections",
        credit_sections=[r"DEPOSITS AND ADDITIONS"],
        debit_sections=[r"ATM & DEBIT CARD WITHDRAWALS", r"ELECTRONIC WITHDRAWALS",
                        r"CHECKS PAID", r"FEES", r"OTHER WITHDRAWALS"],
        stop_markers=[r"DAILY ENDING BALANCE", r"How To Balance Your Checkbook",
                      r"IN CASE OF ERRORS"],
    ),
    Profile(
        key="chase_credit_card",
        display="Chase Credit Card",
        institution="Chase",
        is_credit_card=True,
        fingerprints=[(r"chase\.com", 15), (r"ACCOUNT SUMMARY", 10),
                      (r"PAYMENTS AND OTHER CREDITS", 30),
                      (r"PURCHASE\b", 10), (r"Minimum Payment Due", 20)],
        sign_convention="sections",
        credit_sections=[r"PAYMENTS AND OTHER CREDITS"],
        debit_sections=[r"^PURCHASES?$", r"FEES CHARGED", r"INTEREST CHARGED"],
        stop_markers=[r"INTEREST CHARGES\b.*\bAnnual", r"Information About Your Account"],
        default_direction=-1,
    ),
    Profile(
        key="capital_one_credit",
        display="Capital One Credit Card",
        institution="Capital One",
        is_credit_card=True,
        fingerprints=[(r"capital\s?one", 40), (r"capitalone\.com", 30),
                      (r"Minimum Payment Due", 10)],
        sign_convention="explicit",
        two_date_rows=True,
        require_section=True,
        credit_sections=[r"Payments, Credits and Adjustments"],
        debit_sections=[r":\s*Transactions\b", r"Fees Charged", r"Interest Charged"],
        stop_markers=[r"Totals Year-to-Date", r"Interest Charge Calculation"],
        default_direction=-1,
    ),
    Profile(
        key="chime_checking",
        display="Chime Checking",
        institution="Chime",
        fingerprints=[(r"\bchime\b", 40), (r"Stride Bank|The Bancorp Bank", 20),
                      (r"Checking Account Statement", 45),
                      (r"SETTLEMENT DATE", 10)],
        sign_convention="explicit",
        stop_markers=[],
    ),
    Profile(
        key="chime_credit_card",
        display="Chime Credit Builder Card",
        institution="Chime",
        is_credit_card=True,
        fingerprints=[(r"\bchime\b", 30),
                      (r"Credit Builder (?:Card )?Statement", 55),
                      (r"Card Payment from Secured Account", 20)],
        sign_convention="explicit",
        cc_explicit_orientation=-1,
    ),
    Profile(
        key="navy_federal_checking",
        display="Navy Federal Checking/Savings",
        institution="Navy Federal Credit Union",
        fingerprints=[(r"Navy Federal", 50), (r"navyfederal\.org", 20)],
        has_running_balance=True,
        sign_convention="trailing",
        account_markers=[r"^(.*(?:Savings|Checking|Money Market).*?)\s*-\s*\d+$"],
        stop_markers=[r"Statement of Dividends", r"YEAR-TO-DATE SUMMARY",
                      r"Items\s*Paid"],
    ),
    Profile(
        key="navy_federal_credit",
        display="Navy Federal Credit Card",
        institution="Navy Federal Credit Union",
        is_credit_card=True,
        fingerprints=[(r"Navy Federal", 40), (r"cashRewards|GO REWARDS|Platinum", 15),
                      (r"TRANSACTION\s+POST", 15), (r"Minimum Payment Due", 35),
                      (r"PAYMENT RECEIVED", 15)],
        sign_convention="sections",
        two_date_rows=True,
        credit_sections=[r"PAYMENTS AND CREDITS"],
        debit_sections=[r"TRANSACTIONS\b", r"FEES\b", r"INTEREST CHARGED"],
        default_direction=-1,
    ),
    Profile(
        key="truist_checking",
        display="Truist Business Checking",
        institution="Truist",
        fingerprints=[(r"\btruist\b", 50), (r"Truist\.com", 20)],
        sign_convention="sections",
        # Full-line anchors matter here: the page-1 summary box prints
        # 'Deposits, credits and interest + 32,161.85' — an unanchored
        # prefix match reads that AS the section heading before the real
        # transaction table even starts, mislabeling every debit row until
        # (if ever) the true heading later overwrites it. Some months'
        # PDFs also glue the debit heading's inner spacing
        # ('...debitsandservice charges'), which an exact-space pattern
        # never catches — \s* tolerates either spacing.
        credit_sections=[r"^Deposits,\s*credits\s*and\s*interest$"],
        debit_sections=[r"^Checks$",
                        r"^Other\s*withdrawals,?\s*debits\s*and\s*service\s*charges$"],
        section_overrides_row_words=True,
        # A 'consolidated statement' bundles more than one account (e.g.
        # checking + savings) into a single PDF, each headed by its own
        # bare 'TRUIST <product> <account number>' line before that
        # account's own Account summary. The $-anchor is what keeps this
        # from re-firing on the page-break repeat of the same heading,
        # which is suffixed ' (continued)'.
        account_markers=[r"^TRUIST\s*([A-Za-z ]*(?:CHECKING|SAVINGS)\s*\d{6,})$"],
        stop_markers=[r"Questions, comments or errors"],
    ),
    Profile(
        key="delta_community",
        display="Delta Community Credit Union",
        institution="Delta Community Credit Union",
        fingerprints=[(r"Delta Community", 50), (r"deltacommunitycu", 20)],
        has_running_balance=True,
        sign_convention="trailing",
        account_markers=[r"ID\s+(\d{4}\s+.+?)\s+Balance Forward"],
        stop_markers=[],
    ),
    Profile(
        key="bank_of_america_checking",
        display="Bank of America Checking",
        institution="Bank of America",
        fingerprints=[(r"bankofamerica\.com", 40), (r"Bank of America, N\.A\.", 40),
                      (r"Account summary", 10), (r"Beginning balance on", 20)],
        sign_convention="sections",
        credit_sections=[r"Deposits and other additions"],
        debit_sections=[r"ATM and debit card subtractions",
                        r"Other subtractions", r"^Checks(?:\s*-\s*continued)?$",
                        r"Service fees"],
        section_overrides_row_words=True,
        # 'IMPORTANT INFORMATION' appears as boilerplate on page 2, BEFORE
        # the transaction pages start on page 3 — using it as a stop
        # marker would silently discard every real transaction in the
        # document. Only the true trailer text is safe to stop on.
        stop_markers=[r"Braille and Large Print Request"],
    ),
    Profile(
        key="bank_of_america_credit_card",
        display="Bank of America Credit Card",
        institution="Bank of America",
        is_credit_card=True,
        fingerprints=[(r"Total Credit Line", 40), (r"Payments and Other Credits", 35),
                      (r"Purchases and Adjustments", 25), (r"bankofamerica\.com", 10)],
        sign_convention="sections",
        two_date_rows=True,
        credit_sections=[r"Payments and Other Credits"],
        debit_sections=[r"Purchases and Adjustments", r"^Interest Charged$",
                        r"Fees Charged"],
        section_overrides_row_words=True,
        default_direction=-1,
        # No stop markers: the APR/interest reference table and the closing
        # 'Important Messages' boilerplate contain no dated rows, so the
        # structural date-required rule already keeps them out of the data
        # without risking an early cutoff on a page that still has real
        # content after the trigger phrase (which happened here — see the
        # page-completeness safeguard that caught it).
        stop_markers=[],
    ),
    Profile(
        key="credit_union_of_atlanta_visa",
        display="Credit Union of Atlanta VISA",
        institution="Credit Union of Atlanta",
        is_credit_card=True,
        fingerprints=[(r"CREDIT UNION OF ATLANTA", 50), (r"ezcardinfo", 25),
                      (r"Statement Closing Date", 10)],
        sign_convention="explicit",
        two_date_rows=True,
        # No CREDIT section here: unlike a strictly-sectioned statement,
        # 'Payments, Adjustments and Others' mixes signed credits (trailing
        # minus) with unsigned debits (a reversed reversal prints with no
        # sign, same as a purchase). The explicit trailing sign is the only
        # thing that is ever authoritative; anything unsigned defaults to a
        # debit, same as a purchase would — which 'Transactions' as the
        # sole debit_section, plus default_direction, both agree on.
        debit_sections=[r"^Transactions(\.\.\.\s*Continued)?$"],
        default_direction=-1,
        # The page-1 payment coupon prints the statement closing date next
        # to the New Balance figure and, when no minimum payment is due,
        # the placeholder '** NONE **' — which happens to contain exactly
        # four letters, just clearing the generic no-description filter,
        # so without this the coupon's balance figure reads as a phantom
        # $858.36 transaction dated to the closing date. require_section
        # blocks any dated row before the real 'Transactions' heading is
        # seen on page 2, which the coupon page never prints.
        require_section=True,
        # the mailing-label block at the very top of page 1 is printed in a
        # fake-bold font that duplicates every glyph in the text layer
        # ('KKAABBIIRR HH SSMMAALLLL'); it carries no dollar figures and is
        # never touched by the parser, so it is left alone rather than
        # risking a blanket de-duplication that would corrupt genuinely
        # double-lettered merchant names elsewhere in the document.
        stop_markers=[r"^IMPORTANT INFORMATION$"],
    ),
    Profile(
        key="amazon_business_amex",
        display="Amazon Business Card (American Express)",
        institution="American Express",
        is_credit_card=True,
        fingerprints=[(r"Amazon Business Card", 50), (r"americanexpress\.com", 15),
                      (r"New Standard Bal\. Charges", 25),
                      (r"Payment Terms Balance", 15)],
        sign_convention="explicit",
        credit_sections=[r"^Payments and Credits$"],
        debit_sections=[r"^New Charges$", r"^Fees$", r"^Interest Charged$"],
        # Payments/Credits and New Charges are never mixed-sign the way
        # some issuers' 'adjustments' sections are — a charge titled
        # 'DISPUTE CR REVERSAL' (reversing a credit, itself a debit) still
        # matches the generic 'reversal' credit-word, so section membership
        # must win over that guess here.
        section_overrides_row_words=True,
        default_direction=-1,
        # this issuer prints 'Total Balance', not 'new/ending balance'
        end_labels=[r"total balance"],
        # anchored to the exact standalone heading on the notices page —
        # page 1 references 'IMPORTANT NOTICES' mid-sentence
        # ('Please refer to theIMPORTANT NOTICES section.'), which this
        # pattern must NOT match, since that reference comes before the
        # real transaction pages.
        stop_markers=[r"^IMPORTANT NOTICES$"],
    ),
    Profile(
        key="credit_one_bank",
        display="Credit One Bank Credit Card",
        institution="Credit One Bank",
        is_credit_card=True,
        fingerprints=[(r"CREDIT ONE BANK", 50), (r"CreditOneBank\.com", 20),
                      (r"SUMMARY OF ACCOUNT ACTIVITY", 10)],
        sign_convention="explicit",
        two_date_rows=True,
        credit_sections=[r"^Payments,\s*Credits,?\s*and Adjustments$"],
        debit_sections=[r"^Fees$", r"^Interest Charged$"],
        section_overrides_row_words=True,
        default_direction=-1,
    ),
    Profile(
        key="pnc_business_checking",
        display="PNC Business Checking",
        institution="PNC Bank",
        fingerprints=[(r"PNC Bank", 50), (r"pnc\.com", 25),
                      (r"Business Checking", 10)],
        sign_convention="sections",
        credit_sections=[r"Deposits and Other Additions", r"Interest Paid"],
        debit_sections=[r"Checks and Substitute Checks",
                        r"Banking/Debit Card Withdrawals",
                        r"Online and Electronic Banking Deductions",
                        r"Service Charges and Fees", r"Other Deductions"],
        # note: PNC prints its Daily Balance Detail before the activity
        # detail, so it must NOT be a stop marker; the min-description rule
        # rejects those rows structurally.
        stop_markers=[],
    ),
    Profile(
        key="regions_business_checking",
        display="Regions Bank Business Checking",
        institution="Regions Bank",
        fingerprints=[(r"Regions Bank", 45), (r"1-800-REGIONS", 25),
                      (r"LIFEGREEN BUSINESS CHECKING", 30),
                      (r"regions\.com", 10)],
        sign_convention="sections",
        # Confirmed against two real statements: 'DEPOSITS & CREDITS' and
        # 'WITHDRAWALS' both repeat as '<HEADING> (CONTINUED)' when a
        # section spills onto the next page. 'CHECKS' and 'AUTOMATIC
        # TRANSFERS' are named in the page-1 summary box with their own
        # +/- sign, but neither sample file had any rows in those two
        # sections (both printed $0.00) — add their section headings here
        # once a real file with that activity confirms the exact heading
        # text, rather than guess at it now.
        credit_sections=[r"^DEPOSITS\s*&\s*CREDITS(?:\s*\(CONTINUED\))?$"],
        debit_sections=[r"^WITHDRAWALS(?:\s*\(CONTINUED\))?$",
                        r"^FEES(?:\s*\(CONTINUED\))?$"],
        # 'DAILY BALANCE SUMMARY' is a 3-column date+balance grid printed
        # strictly after Deposits/Withdrawals/Fees are done (confirmed on
        # both sample months) — each cell is a real date next to a real
        # dollar figure, exactly the shape a transaction row scan looks
        # for, so it must be excluded structurally rather than relying on
        # the min-description-length filter to save it.
        stop_markers=[r"DAILY BALANCE SUMMARY"],
    ),
]


# Synchrony Bank private-label cards: one shared layout (Payment Information
# box, Account Summary, an optional Rewards/Fuel-Credit detail page, then
# 'Transaction Detail' with Payments / Purchases and Other Debits / Other
# Credits sub-totals), sold under different retail brand names. Each brand
# gets its own thin profile — same settings, different fingerprints — so
# detection never has to guess between them.
#
# Two structural traps apply to all three:
# 1. Deferred-interest cards (Amazon Store Card) print an 'Account Balance
#    Summary' / 'Promotional Purchase Summary' table BEFORE 'Transaction
#    Detail', full of dates (promo start dates) and dollar figures (balance
#    by promo type) in a shape a date+money row scan could mistake for
#    transactions. require_section=True keeps every dated row structurally
#    excluded until the real 'Payments' / 'Purchases and Other Debits'
#    headings are seen — those promo dates never appear under either.
# 2. Purchases are printed UNSIGNED and Payments/Other Credits are printed
#    explicitly signed negative ('-$750.00'), so a purchase must default to
#    a POSITIVE movement (increases what's owed) for beginning + movements
#    = ending to hold against the printed balances. is_credit_card inverts
#    default_direction=-1 to +1, mirroring Amazon Business Amex's proven
#    'New Charges' handling.
_SYNCHRONY_COMMON = dict(
    is_credit_card=True,
    sign_convention="explicit",
    # The page-1 Account Summary box prints these same words as a plain
    # 'Label - amount' / 'Label + amount' pair with NO '$' sign
    # ('Payments - 400.00 Available Credit $1,153'), right next to a
    # coupon-stub line ('05/14/24 New Balance $5,746.15') that is itself
    # a date + description + money row. Without a tight anchor, 'Payments'
    # matches that summary line first and self.section goes live a whole
    # page early, turning the coupon's balance restatement into a phantom
    # transaction. The real Transaction Detail headings always pair the
    # label with a '$'-prefixed amount and nothing else on the line, so
    # requiring '$' and full-line anchoring excludes the summary box.
    credit_sections=[r"^Payments\s+-?\$[\d,]+\.\d{2}\s*$",
                     r"^Other Credits\s+-?\$[\d,]+\.\d{2}\s*$"],
    debit_sections=[r"^Purchases (?:and|&) Other Debits\s+\$[\d,]+\.\d{2}\s*$"],
    section_overrides_row_words=True,
    default_direction=-1,
    require_section=True,
)

PROFILES.extend([
    Profile(
        key="synchrony_amazon_store_card",
        display="Amazon Store Card (Synchrony Bank)",
        institution="Synchrony Bank",
        fingerprints=[(r"amazon\.syf\.com", 40), (r"Amazon Store Card", 35),
                      (r"syncbank\.com/amazon", 20),
                      (r"Prime Cardholders can earn", 10)],
        **_SYNCHRONY_COMMON,
    ),
    Profile(
        key="synchrony_sams_club_mastercard",
        display="Sam's Club World Mastercard (Synchrony Bank)",
        institution="Synchrony Bank",
        fingerprints=[(r"SamsClubCredit\.com", 40),
                      (r"Sam.s Club World Mastercard", 35),
                      (r"SAM.S CLUB MC/SYNCB", 20)],
        **_SYNCHRONY_COMMON,
    ),
    Profile(
        key="synchrony_techron_advantage",
        display="Techron Advantage Card (Synchrony Bank)",
        institution="Synchrony Bank",
        fingerprints=[(r"TechronAdvantageCard\.com", 40),
                      (r"Chevron or Texaco app", 25),
                      (r"Fuel Credit Details", 15)],
        **_SYNCHRONY_COMMON,
    ),
])

PROFILES.append(
    Profile(
        key="citi_aadvantage_mastercard",
        display="Citi / AAdvantage Executive World Mastercard",
        institution="Citi",
        is_credit_card=True,
        fingerprints=[(r"AADVANTAGE.{0,5}EXECUTIVE WORLD MASTERCARD", 45),
                      (r"citicards\.com", 20), (r"CITI CARDS", 15),
                      (r"AAdvantage.{0,3}Miles Earned", 15)],
        sign_convention="explicit",
        two_date_rows=True,
        # End-anchored, not full-line-anchored: every page prints a small
        # print-imposition code ('033200') in the bottom margin, and on a
        # month where the payments list runs one row longer or shorter
        # than usual, that code's vertical position lands close enough to
        # 'Standard Purchases' to merge into the same visual line
        # ('033200 Standard Purchases') — confirmed on the April statement
        # (which has it) vs. the June statement (which doesn't, because
        # the payments list is one row longer there and pushes the
        # heading further down the page). A full '^...$' anchor missed
        # that merged line entirely, leaving self.section stuck on
        # 'Payments, Credits and Adjustments' for the rest of the page and
        # flipping every purchase to a negative (credit) amount instead of
        # positive — caught by reconciliation failing by exactly 2x the
        # purchases total on the April file.
        credit_sections=[r"Payments,\s*Credits and Adjustments\s*$"],
        # 'Purchases Prior to MM/DD/YY' is a second purchases sub-heading
        # that splits off purchases dated before the new billing cycle
        # started (confirmed on the June statement, spilling onto page 4)
        # — still real, still-owed purchases, not a promotional recap, so
        # it belongs in debit_sections just like 'Standard Purchases'
        # rather than being left to work only by self.section happening
        # not to reset across the page break.
        debit_sections=[r"Standard Purchases\s*$",
                        r"Purchases Prior to \d{1,2}/\d{1,2}/\d{2,4}\s*$",
                        r"Fees Charged\s*$", r"Interest Charged\s*$"],
        default_direction=-1,
        # Page 1 prints 'New balance as of 04/15/26: $373.35' — a real date
        # immediately followed by a real dollar figure on the same line,
        # before the actual transaction table (itself confusingly also
        # headed 'ACCOUNT SUMMARY' on page 3, reusing the page-1 balance
        # box's heading text). require_section keeps that page-1 line from
        # ever being read as a transaction: nothing counts until the real
        # 'Payments, Credits and Adjustments' / 'Standard Purchases'
        # headings are seen.
        require_section=True,
        # Flight-itinerary and hotel-folio continuation lines under a
        # purchase ('NAME: DOUGHTY/FRANKLI', 'ARRIVE: 03/20/26 DEPART:
        # 03/22/26', 'LCH TO DFW : AA: CLASS: I : STOP:X') carry no leading
        # date token of their own, so the row scan already leaves them as
        # plain non-transaction text — confirmed on both real sample files
        # with zero stray rows.
        stop_markers=[r"Interest charge calculation"],
    )
)

PROFILES.append(
    Profile(
        key="chase_marriott_bonvoy_credit",
        display="Chase Marriott Bonvoy Credit Card",
        institution="Chase",
        is_credit_card=True,
        # Distinct fingerprints from the generic chase_credit_card profile
        # so this never gets misdetected as (or steals detection from) a
        # plain Chase card — 'chase.com/marriott' and 'MARRIOTT BONVOY'
        # both extract cleanly even though most of the rest of page 1 does
        # not (see require_section note below).
        fingerprints=[(r"chase\.com/marriott", 45), (r"MARRIOTT BONVOY", 35),
                      (r"Bonvoy Boundless", 10)],
        sign_convention="sections",
        credit_sections=[r"PAYMENTS AND OTHER CREDITS"],
        debit_sections=[r"^PURCHASES?$"],
        default_direction=-1,
        # Page 1 renders a circular 'at a glance' calendar/points graphic
        # whose text elements overlap in the PDF's content stream — the
        # extracted text comes out character-interleaved
        # ('M$i8nim7u9m. P1a7yment Due' for 'Minimum Payment Due
        # $879.17'). None of those interleaved fragments happen to parse
        # as a clean date or money token (confirmed against both real
        # sample files — zero phantom rows either way), but require_section
        # is set anyway as a structural belt-and-braces guard: nothing
        # counts as a transaction until the real 'ACCOUNT ACTIVITY' table's
        # 'PAYMENTS AND OTHER CREDITS' / 'PURCHASE' headings are seen,
        # which the garbled page-1 graphic never prints. The Account
        # Summary balance box just below the graphic (Previous Balance,
        # Payment/Credits, Purchases, New Balance) extracts perfectly
        # clean on both files — only bold section TITLES are
        # character-doubled, not the data lines under them.
        require_section=True,
        # No stop markers: unlike the generic chase_credit_card layout,
        # this issuer prints the 'Information About Your Account'
        # disclosure page (page 2) BEFORE the real 'ACCOUNT ACTIVITY'
        # transaction table (page 3), not after — copying
        # chase_credit_card's stop_markers verbatim would have stopped
        # parsing on page 2 and silently dropped every real transaction.
        # The interest-rate table that follows the transactions on page 3
        # has no dated rows ('Purchases 26.49%(v)(d) - 0 - - 0 -'), so the
        # structural date-required rule already keeps it out without a
        # stop marker.
        stop_markers=[],
    )
)


CATCH_ALL = Profile(
    key="catch_all_statement",
    display="Unrecognised Statement (read generically)",
    institution="",
    fingerprints=[],
    sign_convention="explicit",
)


def detect_profile(first_pages_text: str) -> tuple[Profile, int]:
    """Return best profile and a 0-100 confidence."""
    text = first_pages_text
    best, best_score = CATCH_ALL, 0
    for p in PROFILES:
        score = 0
        for pat, w in p.fingerprints:
            if re.search(pat, text, re.IGNORECASE):
                score += w
        if score > best_score:
            best, best_score = p, score
    conf = min(97, best_score + 30) if best_score >= 40 else max(40, best_score)
    if best is CATCH_ALL:
        conf = 50
    return best, conf
