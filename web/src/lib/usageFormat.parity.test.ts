/**
 * Cross-implementation parity for the cost render rule.
 *
 * `cli._render_cost` and `formatCost` are two implementations of one stated
 * rule, in two languages, rendering into two media. Nothing structural stops
 * them drifting, and they already had: the CLI showed four decimals and the
 * dashboard two, which turned a sub-cent 24h figure into a flat `$0.00`.
 *
 * The expectations below are the CLI's actual output over this case list,
 * captured by running `_render_cost` against it. Regenerate with:
 *
 *     uv run python - <<'PY'
 *     from istota.cli import _render_cost
 *     for c in CASES: print(c, _render_cost(c))
 *     PY
 *
 * A change to either side that is not made to both fails here — but only if the
 * table below is right about the CLI, which nothing in this file can check.
 * `tests/test_cli_render_cost.py` holds the same cases against the real
 * `_render_cost`; edit the two together.
 */

import { describe, expect, it } from 'vitest';

import { formatCost } from '$lib/usageFormat';

// [input, what cli._render_cost produces]
const PARITY: [Record<string, number>, string][] = [
  [{}, '—'],
  [{ api: 0 }, '$0.00'],
  [{ api: 1.5 }, '$1.50'],
  [{ api: 0.0004 }, '$0.0004'],
  [{ api: 0.009 }, '$0.0090'],
  [{ api: 0.01 }, '$0.01'],
  [{ api: 1234.5 }, '$1234.50'],
  [{ api: 9.0 }, '$9.00'],
  [{ estimated: 0 }, '—'],
  [{ subscription: 99 }, '—'],
  [{ api: 1, subscription: 2 }, '$1.00 +subscription'],
  [{ estimated: 0, subscription: 1, unknown: 2 }, '—'],
  [{ api: 1.5, estimated: 0, subscription: 9 }, '$1.50 +estimated+subscription'],
];

describe('formatCost parity with the CLI', () => {
  it.each(PARITY)('renders %j the same as the CLI', (input, expected) => {
    expect(formatCost(input)).toBe(expected);
  });
});
