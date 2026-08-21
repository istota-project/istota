/**
 * Rendering rules for token/cost usage.
 *
 * Extracted from the admin page rather than left inline because this is where
 * the cost decision actually lands: everything upstream stores `cost_usd` and
 * `cost_basis` on every row, and the surface is what decides whether a currency
 * figure appears. A dashboard that quietly invents an invoice is worse than one
 * that declines to guess, so the rule is worth testing directly.
 *
 * The CLI applies the same rule in `cli._render_cost`. The two are deliberately
 * separate implementations of one stated rule rather than a shared artifact —
 * they render into different media — but they must not disagree about *when* a
 * dollar sign appears.
 */

import type { AdminStatsUser } from '$lib/api';

export const COST_PLACEHOLDER = '—';

export function formatNumber(n: number): string {
  return n.toLocaleString();
}

/**
 * One rule: no currency unless it is money.
 *
 * A group spanning bases is marked, never summed: an operator switching the
 * CLI's auth mid-window has rows of both kinds, and adding a plan-equivalent to
 * real spend is the misread this design refuses. That `+` marker is keyed on
 * which bases are *present*, not on their magnitude — a catalog estimate is
 * routinely 0.0, and dropping it on magnitude would let a partial dollar figure
 * read as the whole of a group's spend.
 *
 * With no `api` rows there is nothing to qualify, so the placeholder stands on
 * its own: naming the bases behind it made a column of dashes noisier without
 * changing what it says, which is that no money was spent here.
 */
export function formatCost(byBasis: Record<string, number> | undefined): string {
  const bases = byBasis ?? {};
  const real = bases['api'];
  const other = Object.keys(bases)
    .filter((b) => b !== 'api')
    .sort();
  if (real !== undefined && other.length === 0) return `$${formatMoney(real)}`;
  if (real !== undefined) return `$${formatMoney(real)} +${other.join('+')}`;
  return COST_PLACEHOLDER;
}

/**
 * Two decimals, or four when that would round a real figure to nothing.
 *
 * A 24h per-user figure is routinely sub-cent, and at two decimals it renders
 * `$0.00` — indistinguishable from a genuine zero, which is the one thing a
 * cost column must not be ambiguous about. `cli._fmt_money` states the same
 * rule; the two are separate implementations of one rule and must not disagree.
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
