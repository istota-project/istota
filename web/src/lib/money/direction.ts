/**
 * Where a signed amount's colour is decided, and the only place.
 *
 * `--money-{income,expense}` is deliberately off the status scale — an expense
 * is not an error — and two pages were each mapping a sign onto it with their
 * own `.positive` / `.negative` rules. Now that the tiles rendering those
 * amounts are a shared component, a page cannot reach inside to colour one, so
 * the mapping has to be a value rather than a stylesheet. Same reasoning as
 * `lib/health/status.ts`.
 */

/** A CSS colour for a signed amount, or `''` for zero — which is neither. */
export function directionColor(amount: number): string {
  if (amount > 0) return 'var(--money-income)';
  if (amount < 0) return 'var(--money-expense)';
  return '';
}

/** The fixed direction of a figure that is an income or an expense by kind. */
export const INCOME_COLOR = 'var(--money-income)';
export const EXPENSE_COLOR = 'var(--money-expense)';
