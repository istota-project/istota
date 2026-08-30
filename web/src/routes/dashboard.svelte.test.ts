/**
 * The dashboard reading the identity the root layout already resolved
 * (ISSUE-355).
 *
 * Named without the `+` for the reason `layout.svelte.test.ts` is: SvelteKit
 * reserves that prefix in `src/routes/` and `svelte-kit sync` refuses to build
 * a manifest when it finds a name it does not recognize, which takes
 * `npm run check` down with it.
 *
 * ---
 *
 * The page used to ask the server who the user was a second time and give up
 * when the answer did not come. Offline that `getMe()` rejected, the mount
 * returned early, and the whole body — every tile is a static link gated on
 * `user.features`, and the welcome card needs `bot_name` and `contact` — stayed
 * behind a null check while the shell and the "Dashboard" header rendered
 * around nothing. That is the frame with nothing in it that a cold launch in
 * airplane mode landed on once ISSUE-354 let the app boot at all.
 *
 * Mounted through the real layout rather than with a context supplied by the
 * test, because the seam is the thing that broke: the layout publishing what it
 * resolved, and the page reading it instead of asking again.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { User } from '$lib/api';

const SHELL_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) IstotaApp/0.10.0';

function person(overrides: Partial<User['features']> = {}): User {
  return {
    username: 'alice',
    display_name: 'Alice',
    bot_name: 'Zorg',
    is_admin: false,
    contact: { email: 'alice+istota@example.test', talk: true },
    features: {
      chat: true,
      feeds: false,
      location: false,
      money: false,
      health: false,
      briefings: true,
      google_workspace: false,
      google_workspace_enabled: false,
      admin: false,
      ...overrides,
    },
  };
}

const online = writable(true);
const getMe = vi.fn<() => Promise<User>>();
const getProfile = vi.fn<() => Promise<{ profile: { timezone?: string } | null }>>();
const readUser = vi.fn<(userId: string | null) => Promise<User | null>>();
const seedUserId = vi.fn<() => string | null>();

vi.mock('$lib/stores/connectivity', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/stores/connectivity')>();
  return { ...actual, online, startConnectivity: () => () => {} };
});

vi.mock('$lib/stores/notifications', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/stores/notifications')>();
  return { ...actual, startNotificationPoll: () => {}, stopNotificationPoll: () => {} };
});

vi.mock('$lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api')>();
  return { ...actual, getMe: () => getMe(), getProfile: () => getProfile() };
});

vi.mock('$lib/offline/db', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/offline/db')>();
  return {
    ...actual,
    readUser: (userId: string | null) => readUser(userId),
    writeUser: async () => {},
  };
});

vi.mock('$lib/offline/lastUser', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/offline/lastUser')>();
  return {
    ...actual,
    seedUserId: () => seedUserId(),
    rememberLastUserId: () => {},
    forgetLastUserId: () => {},
  };
});

// Imported after the mocks, so the components' own imports resolve to them.
const DashboardInLayout = (await import('./dashboardInLayout.test.svelte')).default;

beforeEach(() => {
  vi.clearAllMocks();
  online.set(true);
  seedUserId.mockReturnValue('alice');
  readUser.mockResolvedValue(null);
  getProfile.mockResolvedValue({ profile: { timezone: 'Europe/Warsaw' } });
  Object.defineProperty(navigator, 'userAgent', { value: SHELL_UA, configurable: true });
});

afterEach(() => {
  cleanup();
});

describe('the dashboard with no connection', () => {
  beforeEach(() => {
    online.set(false);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));
    getProfile.mockRejectedValue(new TypeError('Failed to fetch'));
    readUser.mockResolvedValue(person());
  });

  it('renders its tiles from the identity the layout resolved', async () => {
    render(DashboardInLayout);

    // The tile descriptions rather than the titles: "Chat" and "Briefings" are
    // also nav links, so the title alone would pass on a page body that painted
    // nothing at all — which is the bug.
    expect(await screen.findByText('Talk to Istota in the app')).toBeTruthy();
    expect(screen.getByText('Your generated briefings and archive')).toBeTruthy();
  });

  it('shows only the tiles the identity carries', async () => {
    render(DashboardInLayout);
    await screen.findByText('Talk to Istota in the app');

    // `money` is false on this record, and a tile is a link into a module the
    // deployment may not have.
    expect(screen.queryByText('Accounts, transactions, and reports')).toBeNull();
  });

  it('draws the welcome card even though the timezone request cannot answer', async () => {
    // `getProfile()` is a second request and already degrades to '', after
    // which `buildGreeting` falls back to the browser clock. The card is drawn
    // from the identity, so it has everything it needs offline.
    const { container } = render(DashboardInLayout);
    await screen.findByText('Talk to Istota in the app');

    await waitFor(() => expect(container.querySelector('.welcome-card')).not.toBeNull());
  });

  it('asks the server who the user is once for the whole app, not once per route', async () => {
    render(DashboardInLayout);
    await screen.findByText('Talk to Istota in the app');

    expect(getMe).toHaveBeenCalledTimes(1);
  });
});

describe('the dashboard when the connection returns', () => {
  it('follows the live identity rather than pinning the cached copy', async () => {
    // The layout swaps a cached identity for the live one on reconnect
    // (ISSUE-354). A page that read the value once at mount would keep showing
    // whatever the cache remembered — the same defect, one layer quieter.
    online.set(false);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));
    readUser.mockResolvedValue(person({ money: false }));

    render(DashboardInLayout);
    await screen.findByText('Talk to Istota in the app');
    expect(screen.queryByText('Accounts, transactions, and reports')).toBeNull();

    getMe.mockResolvedValue(person({ money: true }));
    online.set(true);

    expect(await screen.findByText('Accounts, transactions, and reports')).toBeTruthy();
  });
});

describe('the dashboard with a connection', () => {
  it('renders without a second identity request of its own', async () => {
    getMe.mockResolvedValue(person({ feeds: true }));

    render(DashboardInLayout);

    expect(await screen.findByText('RSS feed reader')).toBeTruthy();
    expect(getMe).toHaveBeenCalledTimes(1);
  });
});
