"""Reconciliation: the proof mechanism.

Beginning + movements must equal ending. Where a running balance is printed
(possibly sparsely, one per day), the accumulated movements between two
printed balances must equal their difference. Detect and report; never
overwrite a figure.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .core import CENT, AccountBlock, Txn

TOL = Decimal("0.01")


def _fmt(x: Optional[Decimal]) -> str:
    return f"{x:,.2f}" if x is not None else "?"


def reconcile_block(b: AccountBlock) -> None:
    """Sets b.recon_passed / b.recon_detail and flags rows where a specific
    group of rows fails its balance-chain check."""
    chain_checked = _chain_check(b)
    total = sum((t.amount for t in b.txns if t.amount is not None), Decimal(0))
    n = sum(1 for t in b.txns if t.amount is not None)

    if b.beginning is not None and b.ending is not None:
        expect = b.beginning + total
        if abs(expect - b.ending) <= TOL:
            b.recon_passed = True
            b.recon_detail = (
                f"Beginning balance {_fmt(b.beginning)} plus {n} movements "
                f"totalling {_fmt(total)} equals the ending balance "
                f"{_fmt(b.ending)}.")
        else:
            b.recon_passed = False
            diff = b.ending - expect
            b.recon_detail = (
                f"Beginning balance {_fmt(b.beginning)} plus {n} movements "
                f"totalling {_fmt(total)} gives {_fmt(expect)}, but the "
                f"statement prints an ending balance of {_fmt(b.ending)} "
                f"(difference {_fmt(diff)}).")
    elif chain_checked:
        b.recon_passed = all(t.status == "OK" for t in b.txns)
        b.recon_detail = ("Proven by the printed running balance: every "
                          "movement matches the balance change."
                          if b.recon_passed else
                          "The printed running balance does not match the "
                          "movements everywhere; the affected rows are marked.")
    else:
        b.recon_passed = None
        b.recon_detail = ("No printed beginning/ending balance pair or "
                          "running balance was found, so the figures cannot "
                          "be proven by arithmetic.")


def _chain_check(b: AccountBlock) -> bool:
    """Accumulate txns between printed balances; check each group.

    Wells Fargo prints one 'ending daily balance' per day: five transactions
    can share one printed figure, so the check is on the group, not the row.
    Returns True if at least one check was performed.
    """
    last_bal: Optional[Decimal] = b.beginning
    group: list[Txn] = []
    checked = False
    for t in b.txns:
        if t.amount is None:
            # cannot chain through an unread amount; restart from next balance
            group = []
            last_bal = None
            if t.balance is not None:
                last_bal = t.balance
            continue
        group.append(t)
        if t.balance is not None:
            if last_bal is not None:
                move = sum(g.amount for g in group if g.amount is not None)
                diff = t.balance - last_bal
                if abs(move - diff) > TOL:
                    if len(group) == 1:
                        t.flag(f"This row reads {t.amount:,.2f} but the printed "
                               f"balance moved by {diff:,.2f} "
                               f"(from {_fmt(last_bal)} to {_fmt(t.balance)}).")
                    else:
                        for g in group:
                            g.flag(f"The {len(group)} rows between printed "
                                   f"balances {_fmt(last_bal)} and "
                                   f"{_fmt(t.balance)} sum to {move:,.2f}, "
                                   f"but the balance moved by {diff:,.2f}.")
                checked = True
            last_bal = t.balance
            group = []
    return checked


def check_section_totals(blocks: list[AccountBlock],
                         totals: list[tuple[AccountBlock, str, Decimal]]
                         ) -> list[str]:
    """Compare printed section totals ('Total Deposits and Additions $X')
    against our sums. Returns human-readable mismatch notes (file level).

    Each printed total is checked ONLY against the rows in the account
    block that was active when it was printed -- a consolidated statement
    (checking + savings in one PDF) prints its own 'Deposits, credits and
    interest' total per account, and pooling every block's rows together
    made one account's total look wrong by exactly the other account's sum.
    """
    notes = []
    for block, name, amount in totals:
        member = [t for t in block.txns if t.amount is not None
                  and t.section and name.lower()[:20] in t.section.lower()]
        if not member:
            continue
        s = sum(abs(t.amount) for t in member)
        if abs(s - amount) > TOL:
            notes.append(
                f"Printed total for '{name}' in account "
                f"'{block.name or '(unnamed)'}' is {amount:,.2f} but the "
                f"extracted rows sum to {s:,.2f}.")
    return notes
