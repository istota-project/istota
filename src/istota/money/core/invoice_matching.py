"""Match incoming payments from a Monarch sync against open invoices (ISSUE-083).

A client pays an invoice, the credit lands in the bank feed, and the sync books
it into the ledger — but the invoice stays open until someone runs
``invoice paid`` by hand. This module closes that loop for the unambiguous case
and refuses to guess at everything else.

The rule is deliberately narrow. A payment settles an invoice only when
**exactly one** open invoice fits it: same amount (to the cent, or within an
explicit tolerance) and issued no later than the payment. Two invoices that fit
one payment, or two payments that fit one invoice, are reported for review
rather than resolved — a wrong auto-match is a money error, and the cost of
asking is one line of output.

Everything here is a pure function over plain data. The caller supplies the
payments and the open invoices, and decides what to do with the verdicts; see
``cli._apply_invoice_matching`` for the wiring.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Payment:
    """An incoming credit newly booked by a sync.

    ``amount`` follows the Monarch/ledger convention: positive is money in.
    Debits are filtered out by the matcher rather than by the caller.
    """
    date: date
    amount: float
    payee: str = ""


@dataclass
class OpenInvoice:
    """An unpaid invoice, reduced to what matching needs.

    ``date`` is a lower bound on when the invoice was issued, not the issue
    date — see ``cli._open_invoices``, which is the only thing that builds
    these. The matcher treats it as "the invoice cannot have existed before
    this", which is all the bound supports.
    """
    number: str
    client: str
    date: date
    total: float


@dataclass
class PaymentMatch:
    """The verdict for one payment.

    ``status`` is one of:

    * ``matched`` — exactly one open invoice fits; ``invoice_number`` names it.
    * ``review`` — more than one invoice fits this payment, or more than one
      payment fits the invoice that would otherwise have been chosen.
      ``candidates`` lists what was in contention.
    * ``no_match`` — nothing fits, or the row was not an incoming credit at
      all. This is the normal outcome for the great majority of transactions
      and is not worth reporting.
    """
    payment: Payment
    status: str
    invoice_number: str | None = None
    candidates: list[str] = field(default_factory=list)
    note: str = ""


def _cents(amount: float) -> int:
    """Money as an integer number of cents.

    Invoice totals are a sum of floats, so ``0.1 + 0.2`` has to compare equal
    to ``0.30``. Comparing rounded cents removes that whole class of
    false negatives without a Decimal conversion at every boundary.
    """
    return int(round(amount * 100))


def match_payments_to_invoices(
    payments: list[Payment],
    open_invoices: list[OpenInvoice],
    tolerance: float = 0.0,
) -> list[PaymentMatch]:
    """Pair incoming credits with the open invoices they settle.

    Args:
        payments: Newly booked transactions. Debits and zero-amount rows are
            never matched, but still get a ``no_match`` verdict so the result
            stays one-to-one with the input.
        open_invoices: Unpaid invoices, in whatever order.
        tolerance: Absolute dollar slack allowed between a payment and an
            invoice total. Defaults to exact.

    Returns one :class:`PaymentMatch` per payment, in input order. Callers
    rely on that pairing to attribute a verdict back to the transaction it
    came from, so never filter the input list here.
    """
    # `not >= 0` rather than `< 0`, so NaN is rejected here instead of
    # reaching `_cents` and raising out of a sync that already wrote a ledger.
    if not (tolerance >= 0) or math.isinf(tolerance):
        raise ValueError(f"tolerance must be a non-negative number, got {tolerance!r}")
    slack = _cents(tolerance)

    matches: list[PaymentMatch] = []
    for payment in payments:
        if payment.amount <= 0:
            matches.append(PaymentMatch(
                payment=payment, status="no_match",
                note="not an incoming credit",
            ))
            continue

        paid = _cents(payment.amount)
        candidates = [
            inv for inv in open_invoices
            # An invoice issued after the money arrived did not cause it.
            if inv.date <= payment.date and abs(_cents(inv.total) - paid) <= slack
        ]
        numbers = sorted(inv.number for inv in candidates)

        if not numbers:
            matches.append(PaymentMatch(
                payment=payment, status="no_match",
                note="no open invoice at this amount",
            ))
        elif len(numbers) == 1:
            matches.append(PaymentMatch(
                payment=payment, status="matched",
                invoice_number=numbers[0], candidates=numbers,
            ))
        else:
            matches.append(PaymentMatch(
                payment=payment, status="review", candidates=numbers,
                note=f"{len(numbers)} open invoices fit this payment",
            ))

    _demote_contested(matches)
    return matches


def _demote_contested(matches: list[PaymentMatch]) -> None:
    """Send every payment claiming a shared invoice to review, in place.

    Per-payment counting can hand the same invoice to two payments — a
    duplicate credit, or two clients who happen to owe the same amount. Each
    sees one candidate and each looks unambiguous on its own. Settling one
    invoice against two payments is the worst outcome available here, so both
    lose the match instead.
    """
    claim_counts: dict[str, int] = {}
    for match in matches:
        if match.status == "matched" and match.invoice_number:
            claim_counts[match.invoice_number] = claim_counts.get(match.invoice_number, 0) + 1

    for match in matches:
        if match.status != "matched" or not match.invoice_number:
            continue
        if claim_counts.get(match.invoice_number, 0) > 1:
            match.status = "review"
            match.note = f"{claim_counts[match.invoice_number]} payments fit {match.invoice_number}"
            match.invoice_number = None


def summarize_matches(
    matches: list[PaymentMatch],
    open_invoices: list[OpenInvoice] | None = None,
) -> dict:
    """Render verdicts as the JSON the CLI reports.

    ``no_match`` rows are dropped — most credits are not invoice payments, and
    listing them would bury the two lines that matter. Returns an empty dict
    when there is nothing to say, so the caller can omit the key entirely.

    Pass ``open_invoices`` to name the client on each invoice mentioned. A
    review line reading "INV-000124 or INV-000125" is not actionable on its
    own; the same line with the two client names usually is.
    """
    client_by_number = {inv.number: inv.client for inv in open_invoices or []}

    matched = []
    review = []
    for match in matches:
        row = {
            "date": match.payment.date.isoformat(),
            "amount": round(match.payment.amount, 2),
            "payee": match.payment.payee,
        }
        if match.status == "matched":
            matched.append({
                **row,
                "invoice_number": match.invoice_number,
                "client": client_by_number.get(match.invoice_number, ""),
            })
        elif match.status == "review":
            review.append({
                **row,
                "candidates": match.candidates,
                "candidate_clients": [
                    client_by_number.get(n, "") for n in match.candidates
                ],
                "reason": match.note,
            })

    summary = {}
    if matched:
        summary["matched"] = matched
    if review:
        summary["review"] = review
    return summary
