/**
 * Rendering rules for token/cost usage.
 *
 * Extracted from the admin page rather than left inline because this is where
 * the cost decision actually lands: everything upstream stores `cost_usd` and
 * `cost_basis` on every row, and the surface is what decides whether a currency
 * figure appears. A dashboard that quietly invents an invoice is worse than one
 * that declines to guess, so the rule is worth testing directly.
 *
 * Python applies the same rule in `usage_render.render_cost`, which the CLI and
 * the `!usage` chat command both import. The two are deliberately separate
 * implementations of one stated rule rather than a shared artifact — they
 * render into different media — but they must not disagree about *when* a
 * dollar sign appears, or about what follows it.
 */

import type { AdminStatsUser } from '$lib/api';

export const COST_PLACEHOLDER = '—';

export function formatNumber(n: number): string {
  return n.toLocaleString();
}

/**
 * One rule: no currency unless it is money.
 *
 * A group spanning bases shows its `api` total and nothing else. Nothing is
 * summed across bases — an operator switching the CLI's auth mid-window has
 * rows of both kinds, and adding a plan-equivalent to real spend is the misread
 * this design refuses.
 *
 * The other bases are not named. A `+estimated+subscription+unknown` suffix
 * used to follow the figure, and it was wrong on two counts: it overflowed a
 * fixed-width column into the one beside it, and it asked the reader to act on
 * a distinction they have no way to act on. What a cost column is for is the
 * money; the rest of the breakdown is still on the wire in `cost_by_basis` for
 * anything that wants it.
 *
 * With no `api` rows at all there is no money to report, so the placeholder
 * stands alone — which on a subscription deployment is most of the time.
 */
export function formatCost(byBasis: Record<string, number> | undefined): string {
  const real = (byBasis ?? {})['api'];
  if (real !== undefined) return `$${formatMoney(real)}`;
  return COST_PLACEHOLDER;
}

/**
 * Two decimals, or four when that would round a real figure to nothing.
 *
 * A 24h per-user figure is routinely sub-cent, and at two decimals it renders
 * `$0.00` — indistinguishable from a genuine zero, which is the one thing a
 * cost column must not be ambiguous about. `usage_render.fmt_money` states the
 * same rule; the two are separate implementations of one rule and must not
 * disagree.
 */
function formatMoney(value: number): string {
  if (value !== 0 && Math.abs(value) < 0.01) return value.toFixed(4);
  return value.toFixed(2);
}

/** Null, never zero, when unmeasured — a zero would be a measurement. */
export function formatContext(n: number | null | undefined): string {
  return n === null || n === undefined ? COST_PLACEHOLDER : formatNumber(Math.round(n));
}

export function formatPercent(n: number | null | undefined): string {
  return n === null || n === undefined ? COST_PLACEHOLDER : `${(n * 100).toFixed(1)}%`;
}

/**
 * The breakdown that explains a user's usage row count.
 *
 * `usage_rows_24h` exceeding `tasks_last_24h` is by design — the column
 * includes spend with no task row at all (a nightly sleep cycle, health OCR) —
 * so the breakdown rides the same cell as a tooltip. Without it the row reads as
 * an arithmetic error.
 */
export function usageOriginTitle(u: AdminStatsUser): string {
  const parts = Object.entries(u.usage_by_origin_24h ?? {})
    .sort((a, b) => b[1].tokens - a[1].tokens)
    .map(([origin, v]) => `${origin}: ${formatNumber(v.tokens)}`);
  const head = `${u.usage_rows_24h} usage row(s)`;
  const unmeasured =
    u.usage_unmeasured_24h > 0 ? ` · ${u.usage_unmeasured_24h} unmeasured task(s)` : '';
  return parts.length > 0 ? `${head}${unmeasured} — ${parts.join(', ')}` : `${head}${unmeasured}`;
}
