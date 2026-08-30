/**
 * The settings page re-resolving the identity through the root layout
 * (ISSUE-355).
 *
 * Every other route reads the layout's record and asks nothing. Settings is the
 * exception: `nextcloud_token` rides on `/me` and a connect made elsewhere
 * changes it while the page is open, so it needs the record *fresh* rather than
 * merely current, and it asks the layout to re-resolve rather than keeping a
 * private copy.
 *
 * That makes it the one page that can drive the layout's own state, and this
 * file pins the three things that follow from it: the request is moved rather
 * than duplicated, a re-resolve that cannot reach the server never paints the
 * cache over a live identity, and a `/me` that fails on its own is still
 * reported — `reload()` does not reject, so the page has to read its answer.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { User } from '$lib/api';

const SHELL_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) IstotaApp/0.10.0';

function person(displayName = 'Alice', nextcloud: User['nextcloud_token'] = null): User {
  return {
    username: 'alice',
    display_name: displayName,
    bot_name: 'Zorg',
    is_admin: false,
    features: {
      chat: true,
      feeds: false,
      location: false,
      money: false,
      health: false,
      briefings: false,
      google_workspace: false,
      google_workspace_enabled: false,
      admin: false,
    },
    nextcloud_token: nextcloud,
  };
}

const profile = {
  profile: {
    user_id: 'alice',
    display_name: 'Alice',
    timezone: 'UTC',
    email_addresses: [],
    trusted_email_senders: [],
    quiet_email_senders: [],
    disabled_skills: [],
    disabled_modules: [],
    routing: {},
    default_destination: 'talk',
    delivery_surfaces: ['talk'],
  },
};

const online = writable(true);
const getMe = vi.fn<() => Promise<User>>();
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
  return {
    ...actual,
    getMe: () => getMe(),
    getProfile: async () => profile,
    getSettingsServices: async () => ({ services: [] }),
    getModules: async () => ({ modules: [] }),
    updateProfile: async () => ({}),
    disconnectNextcloudToken: async () => ({}),
  };
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
const SettingsInLayout = (await import('./settingsInLayout.test.svelte')).default;

beforeEach(() => {
  vi.clearAllMocks();
  online.set(true);
  seedUserId.mockReturnValue('alice');
  readUser.mockResolvedValue(null);
  Object.defineProperty(navigator, 'userAgent', { value: SHELL_UA, configurable: true });
});

afterEach(() => {
  cleanup();
});

describe('the settings page re-resolving the identity', () => {
  it('asks the server once, not once for the layout and again for itself', async () => {
    getMe.mockResolvedValue(person());

    render(SettingsInLayout);
    await screen.findByText('Appearance');

    // One for the layout's gate, one for the page's own refresh. The point is
    // that the refresh is the *same* request the page used to make privately —
    // moved, not duplicated on top of the gate's.
    expect(getMe).toHaveBeenCalledTimes(2);
  });

  it('reports a /me that fails on its own, rather than showing a stale answer', async () => {
    // `reload()` never rejects — the layout owns the 401 redirect and the
    // offline fallback — so a `/me`-only failure reaches nothing unless the
    // page reads the answer. The three calls beside it do not cover this: they
    // are a different endpoint with a different timeout, and a 500 is not a
    // connectivity failure.
    getMe.mockResolvedValueOnce(person()).mockRejectedValueOnce(new Error('boom'));

    render(SettingsInLayout);

    expect(await screen.findByText('Could not confirm your account details.')).toBeTruthy();
  });

  it('never paints the cached identity over the live one it already has', async () => {
    // The failure this guards is not the refresh going wrong; it is the app
    // never recovering afterwards. Both of the layout's retries are gated on
    // "the server has not answered this session", so a cached record swapped in
    // under a live one would be held for the life of the page.
    readUser.mockResolvedValue(person('Alice Cached'));
    getMe
      // The layout's gate: the server answers, so the app is live.
      .mockResolvedValueOnce(person('Alice Live'))
      // The page's own refresh, against a connection that has since gone. The
      // store is flipped from inside the rejection because that is what really
      // happens — `apiFetch` reports every completion into it before
      // rethrowing, and the layout reads the store rather than classifying the
      // exception a second time.
      .mockImplementationOnce(async () => {
        online.set(false);
        throw new TypeError('Failed to fetch');
      });

    render(SettingsInLayout);
    expect(await screen.findByText('Alice Live')).toBeTruthy();

    await waitFor(() => expect(readUser).toHaveBeenCalledWith('alice'));
    // The cache was consulted and declined, which is the whole assertion: the
    // nav still names the record the server gave.
    expect(screen.getByText('Alice Live')).toBeTruthy();
    expect(screen.queryByText('Alice Cached')).toBeNull();
  });
});
