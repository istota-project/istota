"""Tests for ``cli._render_cost``, the CLI half of the cost render rule.

The rule is one sentence — no currency unless it is money — implemented twice,
here and in ``web/src/lib/usageFormat.ts``. ``usageFormat.parity.test.ts`` pins
the two together, but it asserts against a *table* of what this function is
believed to produce; nothing in that test executes Python. Without a test on
this side, a change made only to the dashboard leaves the table and the CLI
disagreeing with no failure anywhere.

The cases below therefore mirror the parity table's, and the two must be edited
together.
"""

from __future__ import annotations

import pytest

from istota.cli import COST_PLACEHOLDER, _render_cost


@pytest.mark.parametrize(
    "cost_by_basis",
    [
        {},
        {"subscription": 99.0},
        {"estimated": 0.0},
        {"estimated": 0.0, "subscription": 1.0, "unknown": 2.0},
    ],
    ids=["empty", "subscription", "zero-estimate", "several-bases"],
)
def test_no_api_rows_render_a_bare_placeholder(cost_by_basis):
    """No ``api`` rows means no money, and the dash says so on its own.

    A subscription's 99.0 is a list price and a catalog estimate is routinely
    0.0, so neither may reach the column as currency. Naming the basis beside
    the dash is what this asserts is gone: every no-money group renders
    identically, whatever it spans.
    """
    assert _render_cost(cost_by_basis) == COST_PLACEHOLDER


def test_none_renders_a_placeholder():
    """The callers pass a group's map straight through; it can be absent."""
    assert _render_cost(None) == COST_PLACEHOLDER


def test_api_only_renders_a_dollar_figure():
    assert _render_cost({"api": 1.5}) == "$1.50"


def test_mixed_group_is_marked_never_summed():
    """1.0 + 2.0 = 3.0 is the misread the ``+`` marker exists to prevent.

    An operator who switched the CLI's auth mid-window has rows of both kinds,
    and a plan-equivalent added to real spend reads as an invoice.
    """
    out = _render_cost({"api": 1.0, "subscription": 2.0})

    assert out == "$1.00 +subscription"
    assert "3.00" not in out


def test_mixed_group_names_every_basis_including_a_zero_one():
    """The marker is keyed on presence, not magnitude.

    Dropping ``estimated`` here because it is 0.0 would let ``$1.50`` read as
    the whole of the group's spend when it is only the measured part of it.
    """
    assert (
        _render_cost({"api": 1.5, "unknown": 2.0, "estimated": 0.0, "subscription": 1.0})
        == "$1.50 +estimated+subscription+unknown"
    )
