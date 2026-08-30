/**
 * The root layout's identity gate (ISSUE-354).
 *
 * Named without the `+`, unlike every other file beside it: SvelteKit reserves
 * that prefix, and `svelte-kit sync` refuses to build a manifest at all when it
 * finds one it does not recognize — which takes `npm run check` down with it.
 *
 * ---
 *
 * Everything the offline work built sits under `{@render children()}`, and the
 * layout renders that branch only once `getMe()` has answered. A cold launch
 * with no connection is exactly the case where it cannot, so the whole feature
 * turned on one `catch` here: the cached room list, the cached transcripts, the
 * banner and the send queue were all present, correct and unreachable behind
 * "Failed to load user info".
 *
 * What is asserted is the three-way split that catch now makes. Unreachable
 * with a cached identity paints the app; `AuthError` still ends the session,
 * without exception; anything else — a reachable server saying no, or no cached
 * identity to fall back on — still shows the error, which is the honest answer
 * on a first launch.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { User } from '$lib/api';

const SHELL_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) IstotaApp/0.10.0';
const BROWSER_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15';

function person(username = 'alice', displayName = 'Alice'): User {
  return {
    username,
    display_name: displayName,
    bot_name: 'Istota',
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
  };
}

// The connectivity store is the layout's answer to "was that a gap or a
// server?", so it is the one thing these tests drive directly. A plain
// writable, since `apiFetch` — what normally reports into it — never runs
// behind a mocked `getMe`.
const online = writable(true);
const startNotificationPoll = vi.fn();
const stopNotificationPoll = vi.fn();
const getMe = vi.fn<() => Promise<User>>();
const readUser = vi.fn<(userId: string | null) => Promise<User | null>>();
const writeUser = vi.fn<(userId: string | null, user: User) => Promise<void>>();
const rememberLastUserId = vi.fn();
const forgetLastUserId = vi.fn();
const seedUserId = vi.fn<() => string | null>();

vi.mock('$lib/stores/connectivity', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/stores/connectivity')>();
  return { ...actual, online, startConnectivity: () => () => {} };
});

vi.mock('$lib/stores/notifications', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/stores/notifications')>();
  return { ...actual, startNotificationPoll, stopNotificationPoll };
});

vi.mock('$lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api')>();
  return { ...actual, getMe: () => getMe() };
});

vi.mock('$lib/offline/db', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/offline/db')>();
  return {
    ...actual,
    readUser: (userId: string | null) => readUser(userId),
    writeUser: (userId: string | null, user: User) => writeUser(userId, user),
  };
});

vi.mock('$lib/offline/lastUser', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/offline/lastUser')>();
  return {
    ...actual,
    seedUserId: () => seedUserId(),
    rememberLastUserId: (id: string | null) => rememberLastUserId(id),
    forgetLastUserId: () => forgetLastUserId(),
  };
});

// Imported after the mocks, so the component's own imports resolve to them.
const Layout = (await import('./+layout.svelte')).default;

/** An empty snippet, so what is asserted is the branch and not what fills it. */
const children = (() => {}) as unknown as import('svelte').Snippet;

function useAgent(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}

beforeEach(() => {
  vi.clearAllMocks();
  online.set(true);
  seedUserId.mockReturnValue('alice');
  readUser.mockResolvedValue(null);
  writeUser.mockResolvedValue(undefined);
  useAgent(SHELL_UA);
});

afterEach(() => {
  cleanup();
});

describe('the root layout with no connection', () => {
  it('renders the app from the cached identity when the server is unreachable', async () => {
    online.set(false);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));
    readUser.mockResolvedValue(person());

    render(Layout, { children });

    // The nav is what the `{:else if user}` branch paints, so finding the
    // display name is the branch having been taken — the error branch renders
    // none of it.
    expect(await screen.findByText('Alice')).toBeTruthy();
    expect(screen.queryByText('Failed to load user info')).toBeNull();
    expect(readUser).toHaveBeenCalledWith('alice');
  });

  it('does not start the notification poll it cannot reach', async () => {
    online.set(false);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));
    readUser.mockResolvedValue(person());

    render(Layout, { children });
    await screen.findByText('Alice');

    expect(startNotificationPoll).not.toHaveBeenCalled();
  });

  it('does not write back the identity it just read from the cache', async () => {
    online.set(false);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));
    readUser.mockResolvedValue(person());

    render(Layout, { children });
    await screen.findByText('Alice');

    // Re-storing it would push its own expiry out forever, and re-pointing the
    // pointer at what the pointer answered with would make it self-confirming.
    expect(writeUser).not.toHaveBeenCalled();
    expect(rememberLastUserId).not.toHaveBeenCalled();
  });

  it('still shows the error when there is no cached identity to fall back on', async () => {
    online.set(false);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));
    readUser.mockResolvedValue(null);

    render(Layout, { children });

    expect(await screen.findByText('Failed to load user info')).toBeTruthy();
  });

  it('reads nothing when no pointer says whose cache to read', async () => {
    online.set(false);
    seedUserId.mockReturnValue(null);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));

    render(Layout, { children });

    expect(await screen.findByText('Failed to load user info')).toBeTruthy();
    expect(readUser).not.toHaveBeenCalled();
  });

  it('adopts the live user and starts the poll once the connection returns', async () => {
    online.set(false);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));
    readUser.mockResolvedValue(person('alice', 'Alice'));

    render(Layout, { children });
    await screen.findByText('Alice');

    getMe.mockResolvedValue(person('alice', 'Alice Renamed'));
    online.set(true);

    expect(await screen.findByText('Alice Renamed')).toBeTruthy();
    await waitFor(() => expect(startNotificationPoll).toHaveBeenCalled());
  });

  it('retries from the error page too, not only from a cached boot', async () => {
    // The error page is the one screen with no way out of its own — there is no
    // reload affordance in the shell — so it is the one that most needs this.
    online.set(false);
    seedUserId.mockReturnValue(null);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));

    render(Layout, { children });
    await screen.findByText('Failed to load user info');

    getMe.mockResolvedValue(person());
    online.set(true);

    expect(await screen.findByText('Alice')).toBeTruthy();
    expect(screen.queryByText('Failed to load user info')).toBeNull();
  });

  it('does not let a slow failing load paint over the live one that overtook it', async () => {
    // `getMe` is bounded but not instant, so two loads can be in flight: a
    // reconnect starts a second while the first is still stalling. Without a
    // generation guard the slow one wins whatever it has, which here is a
    // month-old cached record painted over the identity just resolved.
    online.set(false);
    let failStalled: (e: Error) => void = () => {};
    getMe.mockReturnValueOnce(
      new Promise<User>((_resolve, reject) => {
        failStalled = reject;
      }),
    );
    readUser.mockResolvedValue(person('alice', 'Alice Cached'));

    render(Layout, { children });

    getMe.mockResolvedValue(person('alice', 'Alice Live'));
    online.set(true);
    expect(await screen.findByText('Alice Live')).toBeTruthy();

    // Only now does the stalled first load give up, against a store that has
    // gone offline again in the meantime — so its fallback would find the cache.
    online.set(false);
    failStalled(new TypeError('Failed to fetch'));
    await waitFor(() => expect(screen.getByText('Alice Live')).toBeTruthy());
    expect(screen.queryByText('Alice Cached')).toBeNull();
  });

  it('picks the live identity back up when the user returns to the app', async () => {
    // A retry that fails against a server that *answered* leaves the store
    // online, so no further connectivity edge is coming. Without the
    // visibility listener the app would hold the stale identity for the life of
    // the page, silently.
    online.set(false);
    getMe.mockRejectedValue(new TypeError('Failed to fetch'));
    readUser.mockResolvedValue(person('alice', 'Alice Cached'));

    render(Layout, { children });
    await screen.findByText('Alice Cached');

    getMe.mockRejectedValue(new Error('API error: 500'));
    online.set(true);
    await waitFor(() => expect(getMe).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Alice Cached')).toBeTruthy();

    getMe.mockResolvedValue(person('alice', 'Alice Live'));
    document.dispatchEvent(new Event('visibilitychange'));

    expect(await screen.findByText('Alice Live')).toBeTruthy();
  });
});

describe('the root layout with a server that answered', () => {
  it('caches the user it was given, under the id the cache is namespaced by', async () => {
    getMe.mockResolvedValue(person());

    render(Layout, { children });
    await screen.findByText('Alice');

    expect(writeUser).toHaveBeenCalledWith('alice', expect.objectContaining({ username: 'alice' }));
    expect(rememberLastUserId).toHaveBeenCalledWith('alice');
    expect(startNotificationPoll).toHaveBeenCalled();
  });

  it('stores nothing in a browser, where nothing could read it back', async () => {
    // The pointer is written everywhere — one string, nothing personal. The
    // record is not: it carries the user's inbound address, no browser can read
    // it back (`seedUserId` is shell-gated), and "Clear offline data" is behind
    // the same gate, so a browser would have no way to remove it.
    useAgent(BROWSER_UA);
    getMe.mockResolvedValue(person());

    render(Layout, { children });
    await screen.findByText('Alice');

    expect(writeUser).not.toHaveBeenCalled();
    expect(rememberLastUserId).toHaveBeenCalledWith('alice');
  });

  it('shows the error for a reachable server that failed, cached identity or not', async () => {
    // A 500 is not a gap: `apiFetch` reports it as a server that answered, so
    // the store stays online and the cached identity must not stand in for it.
    getMe.mockRejectedValue(new Error('API error: 500'));
    readUser.mockResolvedValue(person());

    render(Layout, { children });

    expect(await screen.findByText('Failed to load user info')).toBeTruthy();
    expect(readUser).not.toHaveBeenCalled();
  });

  it('ends the session on an auth failure, whatever is cached and whatever the store says', async () => {
    // The one path that must not change: a dead session and a dead network are
    // different answers. Offline *and* 401 is still 401.
    online.set(false);
    readUser.mockResolvedValue(person());
    const { AuthError } = await import('$lib/api');
    getMe.mockRejectedValue(new AuthError());

    render(Layout, { children });

    await waitFor(() => expect(forgetLastUserId).toHaveBeenCalled());
    expect(readUser).not.toHaveBeenCalled();
    expect(screen.queryByText('Alice')).toBeNull();
  });
});
