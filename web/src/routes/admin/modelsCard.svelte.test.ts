import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';

import type { AdminModelProvider, AdminStats, User } from '$lib/api';

/**
 * The Models card's provider rows (model-catalogue Stage 8): one per
 * configured provider, from the discovery caches the daemon writes.
 */

vi.mock('$lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api')>();
  return { ...actual, getAdminStats: vi.fn(), avatarUrl: vi.fn(() => 'about:blank') };
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

function stats(providers: AdminModelProvider[]): AdminStats {
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
    },
    models: {
      brain_kind: 'native',
      default_model: 'claude-sonnet-5-5',
      default_effort: null,
      roles: [],
      providers,
    },
  };
}

const ADMIN: User = {
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
  avatars: { user: null, bot: null },
};

async function show(providers: AdminModelProvider[]) {
  vi.mocked(getAdminStats).mockResolvedValue(stats(providers));
  const rendered = render(Harness, { component: Page, user: ADMIN });
  await screen.findByText('Models');
  return rendered;
}

describe('the Models card provider rows', () => {
  it('names each provider with its host, counts and fetch state', async () => {
    await show([
      {
        id: 'anthropic',
        kind: 'anthropic',
        host: 'api.anthropic.com',
        discovers: false,
        discovered_at: null,
        last_error: null,
        models: 9,
        enabled: 9,
      },
      {
        id: 'openrouter',
        kind: 'openrouter',
        host: 'openrouter.ai',
        discovers: true,
        discovered_at: null,
        last_error: 'HTTP 503',
        models: 342,
        enabled: 2,
      },
    ]);
    expect(screen.getByText('Providers')).toBeTruthy();
    expect(screen.getByText('api.anthropic.com')).toBeTruthy();
    expect(screen.getByText(/9 models, 9 enabled/)).toBeTruthy();
    expect(screen.getByText(/list not fetched/)).toBeTruthy();
    expect(screen.getByText(/342 models, 2 enabled/)).toBeTruthy();
    expect(screen.getByText(/not fetched yet/)).toBeTruthy();
    expect(screen.getByText('Last fetch failed: HTTP 503')).toBeTruthy();
  });

  it('draws no provider block for an older backend', async () => {
    await show([]);
    expect(screen.queryByText('Providers')).toBeNull();
  });
});
