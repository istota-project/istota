"""ISSUE-083: auto-match incoming payments from a Monarch sync to open invoices.

The matcher is deliberately conservative. It only ever proposes an invoice
when exactly one open invoice fits the payment; every other outcome is either
silence (the overwhelmingly common case — most credits are not invoice
payments) or a review flag. It never picks between candidates.
"""

from datetime import date

import pytest

from istota.money.core.invoice_matching import (
    OpenInvoice,
    Payment,
    match_payments_to_invoices,
)


def _payment(amount, day=15, payee="Client Transfer"):
    return Payment(date=date(2026, 5, day), amount=amount, payee=payee)


def _invoice(number, total, day=1, client="acme"):
    return OpenInvoice(
        number=number, client=client, date=date(2026, 5, day), total=total,
    )


class TestSingleMatch:
    def test_exact_amount_match_on_one_open_invoice(self):
        """The reported case: one credit, one open invoice for the same amount."""
        matches = match_payments_to_invoices(
            [_payment(4275.00)], [_invoice("INV-000001", 4275.00)],
        )
        assert len(matches) == 1
        assert matches[0].status == "matched"
        assert matches[0].invoice_number == "INV-000001"
        assert matches[0].candidates == ["INV-000001"]

    def test_other_open_invoices_at_other_amounts_are_ignored(self):
        matches = match_payments_to_invoices(
            [_payment(4275.00)],
            [
                _invoice("INV-000001", 4275.00),
                _invoice("INV-000002", 1200.00),
                _invoice("INV-000003", 4275.01),
            ],
        )
        assert matches[0].status == "matched"
        assert matches[0].invoice_number == "INV-000001"

    def test_cent_level_difference_is_not_a_match_by_default(self):
        """Default tolerance is exact — 4275.01 does not settle a 4275.00 invoice."""
        matches = match_payments_to_invoices(
            [_payment(4275.01)], [_invoice("INV-000001", 4275.00)],
        )
        assert matches[0].status == "no_match"
        assert matches[0].invoice_number is None

    def test_float_noise_does_not_defeat_an_exact_match(self):
        """Invoice totals are summed floats; 0.1+0.2 must still match 0.30."""
        matches = match_payments_to_invoices(
            [_payment(0.1 + 0.2)], [_invoice("INV-000001", 0.30)],
        )
        assert matches[0].status == "matched"


class TestTolerance:
    def test_within_tolerance_matches(self):
        matches = match_payments_to_invoices(
            [_payment(4274.50)], [_invoice("INV-000001", 4275.00)], tolerance=1.00,
        )
        assert matches[0].status == "matched"

    def test_outside_tolerance_does_not_match(self):
        matches = match_payments_to_invoices(
            [_payment(4273.00)], [_invoice("INV-000001", 4275.00)], tolerance=1.00,
        )
        assert matches[0].status == "no_match"

    def test_tolerance_boundary_is_inclusive(self):
        matches = match_payments_to_invoices(
            [_payment(4274.00)], [_invoice("INV-000001", 4275.00)], tolerance=1.00,
        )
        assert matches[0].status == "matched"

    def test_negative_tolerance_is_rejected(self):
        with pytest.raises(ValueError):
            match_payments_to_invoices([], [], tolerance=-1.0)

    def test_nan_tolerance_is_rejected(self):
        """`< 0` is False for NaN, so a plain sign check lets it through."""
        with pytest.raises(ValueError):
            match_payments_to_invoices([], [], tolerance=float("nan"))

    def test_infinite_tolerance_is_rejected(self):
        """Otherwise every open invoice matches every credit."""
        with pytest.raises(ValueError):
            match_payments_to_invoices([], [], tolerance=float("inf"))


class TestAmbiguity:
    def test_two_invoices_at_the_same_amount_go_to_review(self):
        """Flag for manual review instead of guessing, per the issue."""
        matches = match_payments_to_invoices(
            [_payment(4275.00)],
            [_invoice("INV-000001", 4275.00), _invoice("INV-000002", 4275.00)],
        )
        assert matches[0].status == "review"
        assert matches[0].invoice_number is None
        assert matches[0].candidates == ["INV-000001", "INV-000002"]

    def test_tolerance_can_pull_a_second_invoice_into_ambiguity(self):
        matches = match_payments_to_invoices(
            [_payment(4275.00)],
            [_invoice("INV-000001", 4275.00), _invoice("INV-000002", 4274.50)],
            tolerance=1.00,
        )
        assert matches[0].status == "review"
        assert len(matches[0].candidates) == 2

    def test_two_payments_contesting_one_invoice_both_go_to_review(self):
        """A duplicate credit must not mark the same invoice paid twice.

        Each payment sees exactly one candidate, so per-payment counting alone
        would call both a match and settle one invoice against two payments.
        """
        matches = match_payments_to_invoices(
            [_payment(4275.00, day=15), _payment(4275.00, day=16)],
            [_invoice("INV-000001", 4275.00)],
        )
        assert [m.status for m in matches] == ["review", "review"]
        assert all(m.invoice_number is None for m in matches)

    def test_two_payments_two_invoices_same_amount_all_review(self):
        matches = match_payments_to_invoices(
            [_payment(500.00, day=15), _payment(500.00, day=16)],
            [_invoice("INV-000001", 500.00), _invoice("INV-000002", 500.00)],
        )
        assert [m.status for m in matches] == ["review", "review"]

    def test_distinct_amounts_match_independently(self):
        matches = match_payments_to_invoices(
            [_payment(500.00), _payment(750.00)],
            [_invoice("INV-000001", 500.00), _invoice("INV-000002", 750.00)],
        )
        assert [m.status for m in matches] == ["matched", "matched"]
        assert [m.invoice_number for m in matches] == ["INV-000001", "INV-000002"]


class TestPaymentFiltering:
    def test_debits_are_never_payments(self):
        """A spend of the same magnitude must not settle an invoice."""
        matches = match_payments_to_invoices(
            [_payment(-4275.00)], [_invoice("INV-000001", 4275.00)],
        )
        assert [m.status for m in matches] == ["no_match"]
        assert matches[0].invoice_number is None

    def test_zero_amount_is_ignored(self):
        matches = match_payments_to_invoices(
            [_payment(0.0)], [_invoice("INV-000001", 0.0)],
        )
        assert [m.status for m in matches] == ["no_match"]
        assert matches[0].invoice_number is None

    def test_verdicts_stay_one_to_one_with_the_input(self):
        """Callers zip verdicts back onto transactions, so nothing may drop.

        A filtered-out debit would shift every later verdict onto the wrong
        transaction and settle an invoice against something else entirely.
        """
        payments = [_payment(-50.00), _payment(4275.00), _payment(0.0)]
        matches = match_payments_to_invoices(
            payments, [_invoice("INV-000001", 4275.00)],
        )
        assert [m.payment for m in matches] == payments
        assert matches[1].invoice_number == "INV-000001"

    def test_payment_predating_the_invoice_is_reported_not_settled(self):
        """An invoice issued after the money landed did not cause that credit.

        It is still worth a line: the amount fits to the cent, so silence
        would read as "nothing here" when the truth is "these two look
        related and only the date says otherwise".
        """
        matches = match_payments_to_invoices(
            [_payment(4275.00, day=1)], [_invoice("INV-000001", 4275.00, day=10)],
        )
        assert matches[0].status == "review"
        assert matches[0].invoice_number is None
        assert matches[0].candidates == ["INV-000001"]
        assert "issued after this payment" in matches[0].note

    def test_a_payment_matching_no_amount_at_all_stays_silent(self):
        """The normal case for most credits; reporting it would bury the rest."""
        matches = match_payments_to_invoices(
            [_payment(99.00, day=1)], [_invoice("INV-000001", 4275.00, day=10)],
        )
        assert matches[0].status == "no_match"

    def test_payment_on_the_invoice_date_is_a_match(self):
        matches = match_payments_to_invoices(
            [_payment(4275.00, day=10)], [_invoice("INV-000001", 4275.00, day=10)],
        )
        assert matches[0].status == "matched"

    def test_no_open_invoices_yields_no_matches_but_keeps_the_payments(self):
        matches = match_payments_to_invoices([_payment(4275.00)], [])
        assert len(matches) == 1
        assert matches[0].status == "no_match"


class TestSummary:
    def test_summarize_splits_paid_from_review(self):
        from istota.money.core.invoice_matching import summarize_matches

        matches = match_payments_to_invoices(
            [_payment(500.00), _payment(750.00), _payment(750.00, day=16), _payment(9.99)],
            [_invoice("INV-000001", 500.00), _invoice("INV-000002", 750.00)],
        )
        summary = summarize_matches(matches)
        assert [m["invoice_number"] for m in summary["matched"]] == ["INV-000001"]
        assert len(summary["review"]) == 2
        assert "no_match" not in summary

    def test_summarize_empty_is_empty(self):
        from istota.money.core.invoice_matching import summarize_matches

        assert summarize_matches([]) == {}

    def test_summarize_names_the_clients_when_given_the_invoices(self):
        from istota.money.core.invoice_matching import summarize_matches

        invoices = [
            _invoice("INV-000001", 500.00, client="acme"),
            _invoice("INV-000002", 500.00, client="northwind"),
            _invoice("INV-000003", 750.00, client="acme"),
        ]
        matches = match_payments_to_invoices(
            [_payment(500.00), _payment(750.00)], invoices,
        )
        summary = summarize_matches(matches, invoices)
        assert summary["matched"][0]["client"] == "acme"
        assert summary["review"][0]["candidate_clients"] == ["acme", "northwind"]

    def test_summarize_without_invoices_leaves_clients_blank(self):
        from istota.money.core.invoice_matching import summarize_matches

        matches = match_payments_to_invoices(
            [_payment(500.00)], [_invoice("INV-000001", 500.00)],
        )
        assert summarize_matches(matches)["matched"][0]["client"] == ""
