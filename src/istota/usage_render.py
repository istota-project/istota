"""The cost and number render rule for token-usage surfaces.

One rule, stated once: a currency figure appears only where
`cost_basis = 'api'`, and nothing is summed across bases. Two Python callers
import it: `istota usage` (`cli.py`) and `!usage` (`commands.py`). It sits here
rather than in `cli.py` for the second of those — `cli.py` is the CLI entry
point with a heavy import graph, which a chat handler on the Talk polling path
cannot import, so the alternative was a third copy of the rule.

`web/src/lib/usageFormat.ts` states the same rule in TypeScript. Two
implementations, one per language, is the situation
`tests/test_cli_render_cost.py` and `web/src/lib/usageFormat.parity.test.ts`
exist to hold in place; a third Python copy inside a surface would make that
job strictly harder.
"""

# A dollar figure renders only for rows whose cost is real money, and it is the
# whole of what the column says. A subscription's list-price equivalent and a
# catalog estimate both read as spend at a glance, and a surface that quietly
# invents an invoice is worse than one that declines to guess — so neither
# reaches the column, as a figure or as a name. `--json` is exempt: the full
# `cost_by_basis` map travels there, so a consumer can apply its own rule.
COST_PLACEHOLDER = "—"


def render_cost(cost_by_basis: dict) -> str:
    """One rule: no currency unless it is money.

    Returns the group's `api` total as a dollar figure, and the bare
    placeholder when there is no `api` row at all. Nothing is summed across
    bases — an operator who switched the CLI's auth mid-window has rows of both
    kinds, and adding a plan-equivalent to real spend is the misread this whole
    design refuses.

    The other bases are not named. A `+estimated+subscription+unknown` suffix
    used to follow the figure; it overflowed the dashboard's fixed-width column
    into the one beside it, and it asked the reader to act on a distinction
    they have no way to act on. The full breakdown stays available in
    `cost_by_basis` — `--json` emits it, and `render_cost` is only the column.
    """
    real = (cost_by_basis or {}).get("api")
    if real is not None:
        return f"${fmt_money(real)}"
    return COST_PLACEHOLDER


def fmt_money(value: float) -> str:
    """Two decimals, or four when that would round a real figure to nothing.

    A 24h per-user figure is routinely sub-cent, and at two decimals it renders
    `$0.00` — indistinguishable from a genuine zero, which is the one thing a
    cost column must not be ambiguous about. `web/src/lib/usageFormat.ts` states
    the same rule for the dashboard; the two are separate implementations of
    one rule and must not disagree.
    """
    if value != 0 and abs(value) < 0.01:
        return f"{value:.4f}"
    return f"{value:.2f}"


def fmt_int(value) -> str:
    return f"{int(value):,}" if value is not None else COST_PLACEHOLDER


def fmt_context(value) -> str:
    if value is None:
        return COST_PLACEHOLDER
    return f"{int(round(value)):,}"
