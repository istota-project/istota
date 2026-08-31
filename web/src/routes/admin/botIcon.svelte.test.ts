import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';

import type { AdminStats, User } from '$lib/api';

/**
 * The Bot icon card at the foot of `/admin`.
 *
 * Three things here are not visible by reading the template.
 *
 * **The divergence copy has two branches and both are load-bearing.** The
 * deployment's bot icon is not the bot account's Nextcloud profile picture and
 * cannot become one — the daemon holds an app password and Nextcloud's avatar
 * route is session-and-CSRF-guarded — so the card names the account rather than
 * implying the two are linked. Where the payload names no account it still has
 * to say they are separate, or a Nextcloud-backed deployment that failed to
 * report a username silently loses the sentence.
 *
 * **Remove is gated on there being something to remove**, read off the shared
 * identity record rather than off local state, because removing an icon is
 * deployment-wide and the record is what the nav and the chat gutter also read.
 *
 * **The picker is replaced by the busy line rather than disabled.** Unmounting
 * `FileDropZone` is what resets the native input inside it, and a browser fires
 * no `change` for a file the input is already holding — so with the zone merely
 * disabled, re-picking the file whose upload just failed would do nothing and
 * say nothing.
 */

vi.mock('$lib/api', () => ({
  getAdminStats: vi.fn(),
  avatarUrl: vi.fn(() => 'about:blank'),
  AVATAR_ACCEPT: 'image/png',
  uploadBotAvatar: vi.fn(),
  deleteBotAvatar: vi.fn(),
}));

import { getAdminStats, uploadBotAvatar, deleteBotAvatar, avatarUrl } from '$lib/api';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

afterEach(cleanup);

const HASH = 'a'.repeat(64);

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

/** The smallest payload the page renders, with the storage section this card reads. */
function stats(nextcloudUsername: string | null = null): AdminStats {
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
      nextcloud_configured: nextcloudUsername !== null,
      nextcloud_mount_healthy: true,
      nextcloud_username: nextcloudUsername,
    },
  };
}

function person(botHash: string | null): User {
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
    avatars: { user: null, bot: botHash },
  };
}

/** Render the page and wait for the card. */
async function show(botHash: string | null = null, nextcloudUsername: string | null = null) {
  vi.mocked(getAdminStats).mockResolvedValue(stats(nextcloudUsername));
  const rendered = render(Harness, { component: Page, user: person(botHash) });
  await screen.findByText('Bot icon');
  return rendered;
}

/** The card's own section, addressed by its heading rather than by position. */
function card(container: HTMLElement): HTMLElement {
  const heading = Array.from(container.querySelectorAll('section.card h2')).find(
    (h) => h.textContent?.trim() === 'Bot icon',
  );
  expect(heading).toBeTruthy();
  return heading!.closest('section.card') as HTMLElement;
}

beforeEach(() => {
  vi.mocked(uploadBotAvatar).mockReset();
  vi.mocked(deleteBotAvatar).mockReset();
  // Module-scoped, so a render in an earlier test leaves its calls behind and
  // the "asks for no image" assertion reads them as this test's.
  vi.mocked(avatarUrl).mockClear();
});

describe('the bot icon card', () => {
  it('offers a picker', async () => {
    const { container } = await show();
    expect(card(container).querySelector('input[type="file"]')).toBeTruthy();
  });

  it('renders the icon when one is set', async () => {
    const { container } = await show(HASH);
    expect(card(container).querySelector('img')).toBeTruthy();
  });

  it('falls back to the chip with no icon, and asks for no image', async () => {
    const { container } = await show(null);
    expect(card(container).querySelector('img')).toBeNull();
    expect(vi.mocked(avatarUrl)).not.toHaveBeenCalled();
  });
});

describe('the Remove control', () => {
  it('is absent when there is no icon to remove', async () => {
    const { container } = await show(null);
    expect(
      Array.from(card(container).querySelectorAll('button')).find(
        (b) => b.textContent?.trim() === 'Remove',
      ),
    ).toBeUndefined();
  });

  it('is offered once an icon is set, and clears it', async () => {
    vi.mocked(deleteBotAvatar).mockResolvedValue({ deleted: true });
    const { container } = await show(HASH);
    const remove = Array.from(card(container).querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Remove',
    );
    expect(remove).toBeTruthy();
    remove!.click();
    await waitFor(() => expect(vi.mocked(deleteBotAvatar)).toHaveBeenCalledTimes(1));
  });

  it('says so when there was nothing to remove', async () => {
    vi.mocked(deleteBotAvatar).mockResolvedValue({ deleted: false });
    const { container } = await show(HASH);
    Array.from(card(container).querySelectorAll('button'))
      .find((b) => b.textContent?.trim() === 'Remove')!
      .click();
    await screen.findByText('There was nothing to remove.');
  });

  it('surfaces a refusal rather than swallowing it', async () => {
    vi.mocked(deleteBotAvatar).mockRejectedValue(new Error('admin only'));
    const { container } = await show(HASH);
    Array.from(card(container).querySelectorAll('button'))
      .find((b) => b.textContent?.trim() === 'Remove')!
      .click();
    await screen.findByText('admin only');
  });
});

describe('the upload', () => {
  /** Put a file through the card's own file input, the way the picker does. */
  async function pick(container: HTMLElement, file: File) {
    const input = card(container).querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  it('sends the picked file with no Save step', async () => {
    vi.mocked(uploadBotAvatar).mockResolvedValue({ hash: HASH, mime: 'image/webp', bytes: 10 });
    const { container } = await show();
    await pick(container, new File(['x'], 'icon.png', { type: 'image/png' }));
    await waitFor(() => expect(vi.mocked(uploadBotAvatar)).toHaveBeenCalledTimes(1));
  });

  it('sends it exactly once', async () => {
    // The row is deployment-wide, so two concurrent PUTs would resolve in
    // either order and the loser would be everyone's icon.
    vi.mocked(uploadBotAvatar).mockResolvedValue({ hash: HASH, mime: 'image/webp', bytes: 10 });
    const { container } = await show();
    await pick(container, new File(['x'], 'icon.png', { type: 'image/png' }));
    await waitFor(() => expect(vi.mocked(uploadBotAvatar)).toHaveBeenCalledTimes(1));
    // Settled, and nothing re-sent the file the effect took out of the zone.
    await waitFor(() => expect(card(container).querySelector('input[type="file"]')).toBeTruthy());
    expect(vi.mocked(uploadBotAvatar)).toHaveBeenCalledTimes(1);
  });

  it('shows the sentence the server sent for a refusal', async () => {
    // A 413 and a 415 both arrive as `{error}`; collapsing them into
    // `API error: 413` is what `putAvatar` stays off `apiFetch` to avoid.
    vi.mocked(uploadBotAvatar).mockRejectedValue(new Error('that image is too large'));
    const { container } = await show();
    await pick(container, new File(['x'], 'icon.png', { type: 'image/png' }));
    await screen.findByText('that image is too large');
  });

  it('replaces the picker while it is going up, rather than disabling it', async () => {
    let settle: (v: { hash: string; mime: string; bytes: number }) => void = () => {};
    vi.mocked(uploadBotAvatar).mockReturnValue(
      new Promise((resolve) => {
        settle = resolve;
      }),
    );
    const { container } = await show();
    await pick(container, new File(['x'], 'icon.png', { type: 'image/png' }));
    await screen.findByText('Saving the icon…');
    expect(card(container).querySelector('input[type="file"]')).toBeNull();
    settle({ hash: HASH, mime: 'image/webp', bytes: 10 });
    await waitFor(() => expect(card(container).querySelector('input[type="file"]')).toBeTruthy());
  });
});

describe('the divergence copy', () => {
  it('names the Nextcloud account when the deployment reports one', async () => {
    const { container } = await show(null, 'istota-bot');
    const text = card(container).textContent ?? '';
    expect(text).toContain('istota-bot');
    expect(text).toContain('does not change');
  });

  it('still says the two are separate when no account is reported', async () => {
    const { container } = await show(null, null);
    const text = card(container).textContent ?? '';
    expect(text).toContain('Nextcloud profile picture');
    expect(text).toContain('does not change');
  });

  it('says the icon applies to the whole deployment', async () => {
    const { container } = await show();
    expect(card(container).textContent).toContain('everyone on this deployment');
  });
});
