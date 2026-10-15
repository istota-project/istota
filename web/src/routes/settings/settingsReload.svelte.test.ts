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
import { render, cleanup, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { User, UserProfile } from '$lib/api';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

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
    default_room: '',
    external_turn_display: 'collapsed',
    delivery_surfaces: ['talk'],
  },
};

const startNavUnreadPoll = vi.fn();
const stopNavUnreadPoll = vi.fn();
vi.mock('$lib/stores/navUnread', () => ({ startNavUnreadPoll, stopNavUnreadPoll }));

const savedPreference = { value: false };
const updateProfile = vi.fn<(patch: Partial<UserProfile>) => Promise<object>>();
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
  const api = {} as ApiDouble;
  await fillApiDouble(api, {
    ...actual,
    getMe: () => getMe(),
    getProfile: async () =>
      structuredClone({
        profile: { ...profile.profile, show_nav_unread_badges: savedPreference.value },
      }),
    getSettingsServices: async () => ({ services: [] }),
    getModules: async () => ({ modules: [] }),
    updateProfile: (patch: Partial<UserProfile>) => updateProfile(patch),
    disconnectNextcloudToken: async () => ({}),
  });
  return api;
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
const PreferenceProbe = (await import('./navPreferenceProbe.test.svelte')).default;

beforeEach(() => {
  vi.clearAllMocks();
  savedPreference.value = false;
  updateProfile.mockImplementation(async (patch) => {
    savedPreference.value = patch.show_nav_unread_badges ?? savedPreference.value;
    return {};
  });
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
    await screen.findByText('Identity');

    // One for the layout's gate, one for the page's own refresh. The point is
    // that the refresh is the *same* request the page used to make privately —
    // moved, not duplicated on top of the gate's.
    expect(getMe).toHaveBeenCalledTimes(2);
  });

  it('shows the Security card from the identity loaded by the layout', async () => {
    getMe.mockResolvedValue({
      ...person(),
      auth: { method: 'email', email: 'alice@example.com', can_change_password: false },
    });
    render(SettingsInLayout);
    expect(await screen.findByRole('heading', { name: 'Security' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Set a password' })).toBeTruthy();
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

describe('navigation preference through the root identity', () => {
  const label = /Show unread counts in navigation/;

  beforeEach(() => {
    getMe.mockImplementation(async () => ({
      ...person(),
      show_nav_unread_badges: savedPreference.value,
    }));
  });

  it('defaults off and saves only the changed field before reloading the root identity', async () => {
    const view = render(SettingsInLayout, { component: PreferenceProbe });
    const checkbox = await screen.findByRole('checkbox', { name: label });
    expect(checkbox).not.toBeChecked();
    expect(startNavUnreadPoll).not.toHaveBeenCalled();
    await fireEvent.click(checkbox);
    expect(screen.getByLabelText('Confirmed navigation preference')).toHaveTextContent('false');
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() =>
      expect(screen.getByLabelText('Confirmed navigation preference')).toHaveTextContent('true'),
    );
    expect(startNavUnreadPoll).toHaveBeenCalledWith('alice');
    expect(updateProfile).toHaveBeenCalledExactlyOnceWith({ show_nav_unread_badges: true });
    expect(getMe).toHaveBeenCalledTimes(3);
    view.unmount();
    render(SettingsInLayout, { component: PreferenceProbe });
    const restored = await screen.findByRole('checkbox', { name: label });
    expect(restored).toBeChecked();
    stopNavUnreadPoll.mockClear();
    await fireEvent.click(restored);
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() =>
      expect(screen.getByLabelText('Confirmed navigation preference')).toHaveTextContent('false'),
    );
    expect(stopNavUnreadPoll).toHaveBeenCalled();
    expect(updateProfile).toHaveBeenLastCalledWith({ show_nav_unread_badges: false });
  });

  it('discarding an unsaved edit leaves the account preference off', async () => {
    const view = render(SettingsInLayout, { component: PreferenceProbe });
    await fireEvent.click(await screen.findByRole('checkbox', { name: label }));
    view.unmount();
    render(SettingsInLayout, { component: PreferenceProbe });
    expect(await screen.findByRole('checkbox', { name: label })).not.toBeChecked();
    expect(updateProfile).not.toHaveBeenCalled();
  });

  it('a save failure keeps the confirmed preference and the edit', async () => {
    updateProfile.mockRejectedValueOnce(new Error('Save refused'));
    render(SettingsInLayout, { component: PreferenceProbe });
    await fireEvent.click(await screen.findByRole('checkbox', { name: label }));
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByText('Save refused')).toBeInTheDocument();
    expect(screen.getByLabelText('Confirmed navigation preference')).toHaveTextContent('false');
    expect(screen.getByRole('checkbox', { name: label })).toBeChecked();
    expect(startNavUnreadPoll).not.toHaveBeenCalled();
    expect(getMe).toHaveBeenCalledTimes(2);
  });

  it('a failed post-save identity reload retains the last confirmed preference', async () => {
    render(SettingsInLayout, { component: PreferenceProbe });
    await fireEvent.click(await screen.findByRole('checkbox', { name: label }));
    getMe.mockRejectedValueOnce(new Error('Reload failed'));
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByText('Could not confirm your account details.')).toBeInTheDocument();
    expect(savedPreference.value).toBe(true);
    expect(startNavUnreadPoll).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Confirmed navigation preference')).toHaveTextContent('false');
  });
});

it('hands a newly confirmed account to the poll owner on identity reload', async () => {
  getMe
    .mockResolvedValueOnce({ ...person(), show_nav_unread_badges: true })
    .mockResolvedValue({ ...person('Bob'), username: 'bob', show_nav_unread_badges: true });
  render(SettingsInLayout);
  await screen.findByText('Identity');
  expect(startNavUnreadPoll.mock.calls).toEqual([['alice'], ['bob']]);
});
