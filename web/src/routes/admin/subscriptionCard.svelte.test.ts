import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';

import type { AdminStats, AdminSubscription } from '$lib/api';

/**
 * The Claude Code subscription card on `/admin`.
 *
 * It is a card of its own, above Token usage rather than inside it, and the
 * four states below are the whole of what it can be — a populated reading, an
 * absent one, a stale one, and one with pay-as-you-go credits enabled. Each is
 * asserted here rather than checked by eye against the dev server: absent is
 * what both server shapes actually show, and the other three a working
 * deployment produces rarely and on nobody's schedule.
 *
 * Two properties are worth more than the rendering:
 *
 * * **The tint is the operator's rule.** `warn_percent` and `high_percent` ride
 *   the payload so this card and `istota doctor` reach the same verdict about
 *   the same number. A literal here would silently ignore a configured
 *   threshold, so the test moves the thresholds rather than the percentages.
 * * **A windowless reading draws no card.** The card is the reading, so with
 *   no window there is nothing to meter and it is absent rather than drawn as
 *   a note saying so. That note used to be here. It became permanent on both
 *   server shapes once the endpoint turned out not to serve the long-lived
 *   setup-token credential they deploy, and it named nothing anyone could act
 *   on. The reason rides `runtime.subscription_usage` now, which returns SKIP
 *   and carries it.
 */

vi.mock('$lib/api', () => ({
  getAdminStats: vi.fn(),
  // The page's bot-icon control imports these; nothing here exercises it.
  AVATAR_ACCEPT: 'image/png',
  uploadBotAvatar: vi.fn(),
  deleteBotAvatar: vi.fn(),
}));

import { getAdminStats } from '$lib/api';
import type { User } from '$lib/api';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

afterEach(cleanup);

// The page reads the identity the root layout resolved rather than fetching one
// (ISSUE-355), so the harness stands in for that layout. Nothing on this card
// turns on what is in the record.
const person: User = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: true,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: false,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: true,
  },
};

const renderPage = () => render(Harness, { component: Page, user: person });

const usageTotals = () => ({
  rows: 0,
  measured_rows: 0,
  billed_input_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  cache_hit_rate: 0,
  cost_by_basis: {},
  avg_initial_context_tokens: null,
  avg_peak_context_tokens: null,
  context_rows: 0,
});

/** The smallest payload the page will render, with the section under test on
 *  it. Every other section is present and empty — the page reads them all. */
function stats(subscription: AdminSubscription | undefined): AdminStats {
  return {
    system: {
      version: '0.0.0-test',
      uptime_seconds: 0,
      db_size_bytes: 0,
      python_version: '3.12.0',
      last_scheduler_run: null,
      scheduler_healthy: true,
    },
    users: [],
    scheduler: { jobs_total: 0, jobs_active: 0, jobs_paused: 0, jobs: [], last_errors: [] },
    modules: {},
    usage: { totals_24h: usageTotals(), totals_30d: usageTotals() },
    subscription,
    tasks: {
      total: 0,
      last_24h: 0,
      avg_per_day_30d: 0,
      by_source: {},
      failed_by_source_24h: {},
      avg_duration_seconds: 0,
      error_rate_24h: 0,
      failed_24h: 0,
      interactive_24h: 0,
      automated_24h: 0,
      interactive_avg_per_day_30d: 0,
      automated_avg_per_day_30d: 0,
    },
    storage: {
      db_size_bytes: 0,
      backups_count: 0,
      last_backup: null,
      nextcloud_configured: false,
      nextcloud_mount_healthy: false,
    },
  };
}

const window_ = (
  over: Partial<AdminSubscription['windows'] extends (infer W)[] | undefined ? W : never> = {},
) => ({
  key: 'session',
  label: '5-hour',
  percent: 40,
  resets_at: '2026-08-22T18:07:33Z',
  resets_in_seconds: 3847,
  severity: 'normal',
  is_active: true,
  ...over,
});

const populated = (over: Partial<AdminSubscription> = {}): AdminSubscription => ({
  available: true,
  windows: [window_()],
  spend: {
    enabled: false,
    used_minor: 0,
    limit_minor: 2000,
    currency: 'USD',
    exponent: 2,
    percent: 0,
  },
  fetched_at: new Date(Date.now() - 40_000).toISOString(),
  stale: false,
  token_source: 'env',
  warn_percent: 80,
  high_percent: 95,
  error: '',
  ...over,
});

/** Render the page with one subscription payload and wait for the card. */
async function show(subscription: AdminSubscription) {
  vi.mocked(getAdminStats).mockResolvedValue(stats(subscription));
  const { container } = renderPage();
  await screen.findByText('Claude Code subscription');
  return container;
}

/**
 * Render the page with a payload that draws no card, waiting for the page
 * instead of for the card.
 *
 * `show` waits on the card's own heading, which never arrives here, so it can
 * only time out. The anchor is the refresh note: it sits at the foot of the
 * `{:else if stats}` branch under no guard of its own, so reaching it proves
 * the payload was applied and the page settled. That is what separates "the
 * guard dropped the card" from "the page never rendered", which is the way an
 * absence assertion is usually made vacuous.
 */
async function showAbsent(subscription: AdminSubscription | undefined) {
  vi.mocked(getAdminStats).mockResolvedValue(stats(subscription));
  const { container } = renderPage();
  await screen.findByText('Auto-refreshes every 60s.');
  return container;
}

/** The card's own section, addressed by its heading rather than by position.
 *  Null when the page drew no card at all. */
function findCard(container: HTMLElement): HTMLElement | null {
  const headings = Array.from(container.querySelectorAll('section.card h2'));
  const heading = headings.find((h) => h.textContent?.trim() === 'Claude Code subscription');
  return heading ? (heading.closest('section.card') as HTMLElement) : null;
}

function card(container: HTMLElement): HTMLElement {
  const el = findCard(container);
  expect(el, 'the subscription card is on the page').toBeTruthy();
  return el!;
}

/**
 * The card's own money rendering, computed rather than written out.
 *
 * `Intl.NumberFormat(undefined, …)` follows the runtime's default locale, so a
 * literal `'4.65'` here asserts that whoever runs the suite is on en-US: under
 * `LC_ALL=de_DE` the card renders `4,65 $` and the test fails on a card that is
 * behaving correctly.
 */
const money = (value: number, currency: string, digits: number): string =>
  new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);

const findTile = (el: HTMLElement, label: string): Element | undefined =>
  Array.from(el.querySelectorAll('.stat-tile')).find(
    (t) => t.querySelector('.micro-label')?.textContent?.trim() === label,
  );

const tileValue = (el: HTMLElement, label: string): HTMLElement => {
  const tile = findTile(el, label);
  expect(tile, `a tile labelled ${label}`).toBeTruthy();
  return tile!.querySelector('.stat-value') as HTMLElement;
};

/** A tile's whole text — label, value and sub-line. Extra usage splits its
 *  figures across the value and the sub, so most of its assertions want both. */
const tileText = (el: HTMLElement, label: string): string => {
  const tile = findTile(el, label);
  expect(tile, `a tile labelled ${label}`).toBeTruthy();
  return tile!.textContent ?? '';
};

beforeEach(() => {
  vi.mocked(getAdminStats).mockReset();
});

describe('the subscription card — populated', () => {
  it('renders one tile per window, with its percentage and its reset', async () => {
    const el = card(await show(populated()));

    expect(tileValue(el, '5-hour').textContent?.trim()).toBe('40%');
    expect(el.textContent).toContain('resets in 1h 04m');
  });

  it('says so rather than going blank when a window has no reset', async () => {
    const el = card(
      await show(populated({ windows: [window_({ resets_at: null, resets_in_seconds: null })] })),
    );

    expect(el.textContent).toContain('no reset scheduled');
  });

  it('reports how old the reading is', async () => {
    const el = card(await show(populated()));

    expect(el.textContent).toContain('Updated 40s ago');
    expect(el.textContent).not.toContain('stale');
  });

  it('sits immediately above the Token usage card', async () => {
    // Two cards, not one strip inside Token usage: the cost column below is all
    // dashes on a subscription deployment, and this is the budget it cannot
    // report. Order is the whole of what makes them read as a pair.
    const container = await show(populated());
    const headings = Array.from(container.querySelectorAll('section.card h2')).map((h) =>
      h.textContent?.trim(),
    );
    const here = headings.indexOf('Claude Code subscription');

    expect(here).toBeGreaterThanOrEqual(0);
    expect(headings[here + 1]).toBe('Token usage');
  });
});

describe('the subscription card — tinting', () => {
  // The percentage is held still and the thresholds are moved, which is the
  // only arrangement that can tell "tinted by the payload" from "tinted by a
  // literal that happens to match the default".
  const tintAt = async (warn: number, high: number) => {
    const el = card(
      await show(
        populated({ windows: [window_({ percent: 60 })], warn_percent: warn, high_percent: high }),
      ),
    );
    return tileValue(el, '5-hour').closest('.stat-tile')?.getAttribute('style') ?? '';
  };

  it('is green below the operator’s warn threshold', async () => {
    expect(await tintAt(80, 95)).toContain('var(--status-success-fg)');
  });

  it('is amber at or above it', async () => {
    expect(await tintAt(55, 95)).toContain('var(--status-warn-fg)');
    cleanup();
    expect(await tintAt(60, 95)).toContain('var(--status-warn-fg)');
  });

  it('is red at or above the high threshold', async () => {
    expect(await tintAt(30, 55)).toContain('var(--status-danger-fg)');
    cleanup();
    expect(await tintAt(30, 60)).toContain('var(--status-danger-fg)');
  });

  it('does not tint at all when the payload names no thresholds', async () => {
    // An older backend. Guessing 80/95 here is exactly how a configured
    // threshold comes to be ignored without anything saying so.
    const el = card(
      await show(
        populated({
          windows: [window_({ percent: 99 })],
          warn_percent: undefined,
          high_percent: undefined,
        }),
      ),
    );
    const style = tileValue(el, '5-hour').closest('.stat-tile')?.getAttribute('style') ?? '';

    expect(style).not.toContain('--status-');
  });

  it('collapses the amber band into danger on an inverted pair', async () => {
    // The loader corrects `warn > high`; a pair arriving past it must still
    // flag, and at the lower of the two. That is the reading doctor reaches
    // too — it has one WARN branch at `min(warn, high)`, so every percentage it
    // flags is one this tints rather than leaving green.
    expect(await tintAt(95, 55)).toContain('var(--status-danger-fg)');
    cleanup();
    expect(await tintAt(95, 80)).toContain('var(--status-success-fg)');
  });

  it('ignores the server’s own severity', async () => {
    // Carried on the wire, deliberately unused: its scale is undocumented, and
    // two surfaces applying one rule to one number always agree.
    const el = card(
      await show(populated({ windows: [window_({ percent: 10, severity: 'critical' })] })),
    );
    const style = tileValue(el, '5-hour').closest('.stat-tile')?.getAttribute('style') ?? '';

    expect(style).toContain('var(--status-success-fg)');
  });
});

describe('the subscription card — absent', () => {
  // Every payload here is one the page must render *around* rather than draw a
  // card for. Each case asserts the page came up, because an absence assertion
  // against a page that never rendered passes for the wrong reason.

  it('draws no card when the key is absent', async () => {
    // The ordinary state on both server shapes, and the one the superseded
    // tests never covered: `subscription` is optional and the backend omits it
    // unless Claude Code is the brain or the fallback and the endpoint
    // returned windows.
    const el = await showAbsent(undefined);

    expect(findCard(el)).toBeNull();
  });

  it('draws no card for a refused credential', async () => {
    const el = await showAbsent({
      available: false,
      windows: [],
      spend: null,
      fetched_at: null,
      stale: false,
      token_source: '',
      warn_percent: 80,
      high_percent: 95,
      error: 'no Claude Code OAuth credential found',
    });

    expect(findCard(el)).toBeNull();
    // The reason is not dropped, it moves. `check_subscription_usage` returns
    // SKIP carrying it; nothing on this page renders it, and the assertion
    // that it reaches an operator lives with that check in
    // `tests/test_doctor.py`. Asserting its absence here is what keeps the two
    // surfaces from both claiming the job.
    expect(el.textContent).not.toContain('no Claude Code OAuth credential found');
  });

  it('draws no card for a section-level exception, and says nothing', async () => {
    // The only windowless payload the server can still emit: the stats
    // endpoint's outer catch writes `{error: str(exc)}` with no other key
    // (`web_app.py`, `payload["subscription"] = {"error": str(exc)}`). The
    // superseded test covered this shape too, asserting the card rendered the
    // reason and fell back to "unreported reason" where `str(exc)` was empty.
    // There is no note to fall back in now, so what is pinned instead is the
    // decision that a failed section degrades to silence on this surface
    // rather than to a card with an empty reason in it.
    const el = await showAbsent({ error: 'AttributeError: boom' });

    expect(findCard(el)).toBeNull();
    expect(el.textContent).not.toContain('boom');
  });

  it('draws no card for an available reading with no windows', async () => {
    // Defensive rather than a wire state: the section returns `None` on
    // `not snapshot.has_data`, so it never reaches the point of emitting a
    // windowless payload, and `available` is a hardcoded `true` below that.
    // The guard reads the windows rather than the flag, so it covers the pair
    // anyway.
    const el = await showAbsent(populated({ windows: [], error: 'the endpoint named no window' }));

    expect(findCard(el)).toBeNull();
  });
});

describe('the subscription card — stale', () => {
  it('shows the numbers and admits they are old', async () => {
    // A stale reading is real numbers from an earlier fetch plus the failure
    // that made them old. Showing the percentage without the admission is the
    // most misleading pair this card could draw.
    const el = card(
      await show(
        populated({
          stale: true,
          error: 'HTTP 503 from the usage endpoint',
          fetched_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
        }),
      ),
    );

    expect(tileValue(el, '5-hour').textContent?.trim()).toBe('40%');
    expect(el.textContent).toContain('Updated 2h ago');
    expect(el.textContent).toContain('reading is stale');
    expect(el.textContent).toContain('HTTP 503 from the usage endpoint');
  });
});

describe('the subscription card — extra usage', () => {
  it('is absent while pay-as-you-go credits are off', async () => {
    const el = card(await show(populated()));

    expect(el.textContent).not.toContain('Extra usage');
  });

  it('reports committed credits as real money when they are on', async () => {
    // Not a contradiction of the rule that keeps a dollar figure off the Token
    // usage card: that refuses to price plan-equivalent tokens at list, while
    // these are credits the account has actually committed.
    const el = card(
      await show(
        populated({
          spend: {
            enabled: true,
            used_minor: 465,
            limit_minor: 2000,
            currency: 'USD',
            exponent: 2,
            percent: 23.25,
          },
        }),
      ),
    );

    // A tile in the grid, not a footer line under it: the card reads as one
    // row of meters, and the spent figure is the tile's value because it is
    // the one number here that is money rather than a share of a quota.
    expect(tileValue(el, 'Extra usage').textContent?.trim()).toBe(money(4.65, 'USD', 2));
    expect(tileText(el, 'Extra usage')).toContain(money(20, 'USD', 2));
    expect(tileText(el, 'Extra usage')).toContain('23.3%');
  });

  it('sits alongside the windows in one grid rather than below it', async () => {
    // The layout the card is asked for: four tiles across, matching the system
    // banner. A regression here is the tile drifting back out of the grid into
    // a note, which the text-content assertions above would not notice.
    const el = card(
      await show(
        populated({
          windows: [
            window_({ key: 'session', label: '5-hour' }),
            window_({ key: 'weekly_all', label: 'Weekly (all models)' }),
            window_({ key: 'weekly_scoped:fable', label: 'Weekly (Fable)' }),
          ],
          spend: {
            enabled: true,
            used_minor: 465,
            limit_minor: 2000,
            currency: 'USD',
            exponent: 2,
            percent: 23.25,
          },
        }),
      ),
    );

    const grid = el.querySelector('.kpi-grid');
    expect(grid, 'the card has a tile grid').toBeTruthy();
    const labels = Array.from(grid!.querySelectorAll('.stat-tile .micro-label')).map((n) =>
      n.textContent?.trim(),
    );
    // Extra usage last, after every window, in one grid.
    expect(labels).toEqual(['5-hour', 'Weekly (all models)', 'Weekly (Fable)', 'Extra usage']);
  });

  it('takes the divisor from the exponent rather than assuming cents', async () => {
    // The removed `!usage` command divided by a hardcoded 100, which is wrong
    // for any currency that is not two-decimal.
    const el = card(
      await show(
        populated({
          spend: {
            enabled: true,
            used_minor: 1500,
            limit_minor: 10000,
            currency: 'JPY',
            exponent: 0,
            percent: 15,
          },
        }),
      ),
    );

    expect(el.textContent).toContain(money(1500, 'JPY', 0));
    expect(el.textContent).toContain(money(10000, 'JPY', 0));
    // Not 1500 cents. The exponent is 0, so the minor unit *is* the yen.
    expect(el.textContent).not.toContain(money(15, 'JPY', 0));
  });

  it('does not clamp an overage back to a full bar', async () => {
    // The one figure on this card that is not a token count.
    // `subscription_usage._unclamped_percent` exists to keep a spend percentage
    // above 100, because that is money already committed — rendering it as 100%
    // would hide the overage while the two money figures beside it still showed
    // one, which is one line contradicting itself.
    const el = card(
      await show(
        populated({
          spend: {
            enabled: true,
            used_minor: 3000,
            limit_minor: 2000,
            currency: 'USD',
            exponent: 2,
            percent: 150,
          },
        }),
      ),
    );

    expect(el.textContent).toContain('150%');
    expect(el.textContent).not.toContain('100%');
  });

  it('still shows the figure when the currency code is malformed', async () => {
    // `Intl` throws on a code that is not three letters (an unknown but
    // well-formed one formats fine), and the number is worth showing anyway.
    const el = card(
      await show(
        populated({
          spend: {
            enabled: true,
            used_minor: 465,
            limit_minor: 2000,
            currency: 'US',
            exponent: 2,
            percent: 23.25,
          },
        }),
      ),
    );

    expect(el.textContent).toContain('4.65 US');
    expect(el.textContent).toContain('20.00 US');
  });
});
