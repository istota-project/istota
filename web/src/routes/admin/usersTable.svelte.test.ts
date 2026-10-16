import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';

import type { AdminStats, AdminStatsUser, User } from '$lib/api';

/**
 * The User column of the `/admin` users table.
 *
 * Two things about the face here are not visible by reading the template.
 *
 * **The admin reading the page is not automatically allowed to see it.**
 * `GET /avatars/user/{id}` answers for the caller themselves or for a
 * co-member of some room, and being an admin is neither — deliberately, since
 * a distinguishable answer would make the endpoint a user-directory oracle
 * (D6). So a row for someone this admin shares no room with 404s and falls
 * back to the chip, which is the same rendering the table had before. The
 * request is not wasted: branch 1 is `max-age=30`, so the whole table costs at
 * most one 404 per user per half-minute.
 *
 * **No version rides in the URL** and cannot: `/me` carries the reader's own
 * hash and the bot's, and nothing carries a third party's (D13). The request
 * goes out bare and revalidates on an ETag.
 */

vi.mock('$lib/api', async () => {
  // Only the fetch is mocked. `avatarUrl` is the real one, so the assertions
  // below are about the URL the browser would request rather than about a
  // call this file recorded against itself.
  const actual = await vi.importActual<typeof import('$lib/api')>('$lib/api');
  return {
    ...actual,
    getAdminStats: vi.fn(),
    uploadBotAvatar: vi.fn(),
    deleteBotAvatar: vi.fn(),
  };
});

import { getAdminStats } from '$lib/api';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

afterEach(cleanup);

function usageTotals() {
  return {
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
  };
}

function member(username: string, displayName: string): AdminStatsUser {
  return {
    username,
    display_name: displayName,
    is_admin: false,
    tasks_total: 3,
    tasks_last_24h: 1,
    tasks_avg_per_day: 1,
    tasks_by_source_24h: {},
    tasks_interactive_24h: 1,
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
  };
}

function stats(users: AdminStatsUser[]): AdminStats {
  return {
    system: {
      version: '0.0.0-test',
      uptime_seconds: 0,
      db_size_bytes: 0,
      python_version: '3.12.0',
      last_scheduler_run: null,
      scheduler_healthy: true,
    },
    users,
    scheduler: { jobs_total: 0, jobs_active: 0, jobs_paused: 0, jobs: [], last_errors: [] },
    modules: {},
    usage: { totals_24h: usageTotals(), totals_30d: usageTotals() },
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
      nextcloud_mount_healthy: true,
      nextcloud_username: null,
    },
  };
}

function reader(): User {
  return {
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
    avatars: { user: 'me77', bot: null },
  };
}

async function show(users: AdminStatsUser[]) {
  vi.mocked(getAdminStats).mockResolvedValue(stats(users));
  const rendered = render(Harness, { component: Page, user: reader() });
  await screen.findByText('Users');
  return rendered;
}

/** The row whose User cell names `displayName`. */
function row(container: HTMLElement, displayName: string): HTMLElement {
  const name = Array.from(container.querySelectorAll('.users-grid .username')).find(
    (n) => n.textContent?.trim() === displayName,
  );
  expect(name, `no row for ${displayName}`).toBeTruthy();
  return name!.closest('tr') as HTMLElement;
}

describe('the admin users table shows a face beside the name', () => {
  it('asks the endpoint for each listed user, keyed on the id', async () => {
    const { container } = await show([member('bob', 'Bob'), member('carol', 'Carol')]);

    expect(row(container, 'Bob').querySelector('img')?.getAttribute('src')).toBe(
      '/api/avatars/user/bob',
    );
    expect(row(container, 'Carol').querySelector('img')?.getAttribute('src')).toBe(
      '/api/avatars/user/carol',
    );
  });

  it('asks bare, since no third party hash is on the wire', async () => {
    const { container } = await show([member('bob', 'Bob')]);
    // Not the reader's own `?v=me77`, which would be an immutable cache entry
    // holding one person's face under another's id.
    expect(row(container, 'Bob').querySelector('img')?.getAttribute('src')).not.toContain('?v=');
  });

  it('keeps the name and the admin badge beside it', async () => {
    const admin = { ...member('dave', 'Dave'), is_admin: true };
    const { container } = await show([admin]);
    const cell = row(container, 'Dave').querySelector('td') as HTMLElement;
    expect(cell.textContent).toContain('Dave');
    expect(cell.querySelector('.admin-badge')?.textContent?.trim()).toBe('admin');
  });

  it('labels the image decoratively — the name is already in the cell', async () => {
    const { container } = await show([member('bob', 'Bob')]);
    expect(row(container, 'Bob').querySelector('img')?.getAttribute('alt')).toBe('');
  });
});

describe('the admin users table counts job runs apart', () => {
  it('shows command and skill runs beside the interactive and automated counts', async () => {
    const bob = { ...member('bob', 'Bob'), tasks_automated_24h: 2, tasks_no_model_24h: 288 };
    const { container } = await show([bob]);
    const summary = row(container, 'Bob').querySelector('.source-summary') as HTMLElement;
    expect(summary.textContent?.replace(/\s+/g, ' ')).toContain('auto 2 · jobs 288');
  });
});

describe('the status page lays its cards out as a masonry wall', () => {
  it('puts every card straight into the grid, with the banner and Users at full width', async () => {
    const { container } = await show([member('bob', 'Bob')]);
    const grid = container.querySelector('.card-grid-tracks.masonry') as HTMLElement;
    expect(grid).toBeTruthy();
    // Masonry places and measures only the grid's own children.
    const cards = Array.from(grid.children);
    expect(cards.length).toBeGreaterThan(3);
    expect(cards.every((c) => c.classList.contains('card'))).toBe(true);
    expect(grid.querySelector(':scope > .system-banner')?.getAttribute('data-width')).toBe('full');
    const users = cards.find((c) => c.querySelector('.users-grid'));
    expect(users?.getAttribute('data-width')).toBe('full');
  });
});
