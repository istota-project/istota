import { describe, expect, it } from 'vitest';

import type { AdminStatsUser } from '$lib/api';
import {
  COST_PLACEHOLDER,
  formatContext,
  formatCost,
  formatPercent,
  usageOriginTitle,
} from '$lib/usageFormat';

describe('formatCost', () => {
  it('renders a dollar figure only for real money', () => {
    expect(formatCost({ api: 1.5 })).toBe('$1.50');
  });

  it('renders a bare placeholder for a plan-equivalent', () => {
    // A subscription figure is a list price, not spend. Showing it as currency
    // is exactly the misread this rule refuses — and the basis name is not
    // carried alongside the dash, which says all a cost column needs to.
    expect(formatCost({ subscription: 99 })).toBe(COST_PLACEHOLDER);
  });

  it('renders a bare placeholder for a catalog estimate, including a zero one', () => {
    // The catalog prices an unknown model at zero, so this is the common shape.
    expect(formatCost({ estimated: 0 })).toBe(COST_PLACEHOLDER);
  });

  it('renders every no-money group identically, however many bases it spans', () => {
    // Nothing distinguishes these to a reader of the cost column: none of them
    // is money. A suffix here would vary the rendering without varying that.
    expect(formatCost({ estimated: 0, subscription: 1, unknown: 2 })).toBe(COST_PLACEHOLDER);
    expect(formatCost({ subscription: 99 })).toBe(COST_PLACEHOLDER);
    expect(formatCost({})).toBe(COST_PLACEHOLDER);
    expect(formatCost(undefined)).toBe(COST_PLACEHOLDER);
  });

  it('shows the api figure alone on a group spanning bases, never the sum', () => {
    // 1.5 + 99 = 100.5, which is what an operator switching auth mid-window
    // would otherwise be shown as their spend. The column reports the 1.5 and
    // says nothing about the 99 — naming it was noise a reader could not act on,
    // and it overflowed the fixed-width cell.
    const out = formatCost({ api: 1.5, subscription: 99 });

    expect(out).toBe('$1.50');
    expect(out).not.toContain('100.5');
    expect(out).not.toContain('subscription');
  });

  it('never puts a basis name in the column', () => {
    // The `+estimated+subscription+unknown` suffix is gone. A dollar figure now
    // means one thing: money actually spent, on rows we can account for.
    const out = formatCost({ api: 1.5, unknown: 2, estimated: 0, subscription: 1 });

    expect(out).toBe('$1.50');
    expect(out).not.toContain('+');
  });

  it('renders a genuine zero of real money as currency', () => {
    // An api row that cost nothing is still a measured figure.
    expect(formatCost({ api: 0 })).toBe('$0.00');
  });

  it('does not round a sub-cent figure to a flat zero', () => {
    // A 24h per-user cost is routinely sub-cent, and `$0.00` there is
    // indistinguishable from a genuine zero — the one thing a cost column
    // must not be ambiguous about. `usage_render.fmt_money` states the same rule.
    expect(formatCost({ api: 0.0004 })).toBe('$0.0004');
    expect(formatCost({ api: 0.009 })).toBe('$0.0090');
  });

  it('keeps two decimals from a cent upwards', () => {
    expect(formatCost({ api: 0.01 })).toBe('$0.01');
    expect(formatCost({ api: 1234.5 })).toBe('$1234.50');
  });
});

describe('formatContext', () => {
  it('renders a placeholder rather than a zero when unmeasured', () => {
    // Null, never zero. A zero would be a measurement, and the whole reason
    // these columns are nullable is that SQL AVG skips NULL.
    expect(formatContext(null)).toBe(COST_PLACEHOLDER);
    expect(formatContext(undefined)).toBe(COST_PLACEHOLDER);
  });

  it('rounds and groups a measured value', () => {
    expect(formatContext(14433.6)).toBe((14434).toLocaleString());
  });

  it('renders a measured zero as a zero', () => {
    expect(formatContext(0)).toBe('0');
  });
});

describe('formatPercent', () => {
  it('renders a placeholder for an unmeasured rate', () => {
    expect(formatPercent(null)).toBe(COST_PLACEHOLDER);
  });

  it('renders a rate to one decimal', () => {
    expect(formatPercent(0.4886)).toBe('48.9%');
  });
});

describe('usageOriginTitle', () => {
  const user = (over: Partial<AdminStatsUser>): AdminStatsUser =>
    ({
      username: 'alice',
      display_name: 'alice',
      is_admin: false,
      tasks_total: 0,
      tasks_last_24h: 0,
      tasks_avg_per_day: 0,
      tasks_by_source_24h: {},
      tasks_interactive_24h: 0,
      tasks_automated_24h: 0,
      tasks_failed_24h: 0,
      last_active: null,
      usage_tokens_24h: 0,
      usage_tokens_30d: 0,
      usage_cost_24h: {},
      usage_cost_30d: {},
      usage_by_origin_24h: {},
      usage_avg_initial_context: null,
      usage_avg_peak_context: null,
      usage_cache_hit_rate_24h: 0,
      usage_rows_24h: 0,
      usage_unmeasured_24h: 0,
      ...over,
    }) as AdminStatsUser;

  it('names the origins behind a row count, largest first', () => {
    const out = usageOriginTitle(
      user({
        usage_rows_24h: 3,
        usage_by_origin_24h: {
          task: { rows: 1, tokens: 100 },
          sleep_cycle: { rows: 2, tokens: 900 },
        },
      }),
    );

    expect(out).toContain('3 usage row(s)');
    expect(out.indexOf('sleep_cycle')).toBeLessThan(out.indexOf('task'));
  });

  it('names the unmeasured tasks when there are any', () => {
    const out = usageOriginTitle(user({ usage_rows_24h: 1, usage_unmeasured_24h: 2 }));

    expect(out).toContain('2 unmeasured task(s)');
  });

  it('omits the unmeasured clause when there are none', () => {
    const out = usageOriginTitle(user({ usage_rows_24h: 1 }));

    expect(out).not.toContain('unmeasured');
  });
});
