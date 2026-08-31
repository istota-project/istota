/**
 * The settings row that clears what the app keeps on the device (ISSUE-202).
 *
 * The assertion that matters is the gate. The row promises something only the
 * native shell has — an app that opens with no connection, served by a service
 * worker — and offering it in a browser would name a mechanism that is not
 * there and a failure the reload button already fixes. It is gated on the
 * shell version that declares the app-bound domains WebKit needs before it
 * will run a worker at all, so an older app is offered nothing it could act
 * on either.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  getSettingsServices: vi.fn(async () => ({ services: [] })),
  getModules: vi.fn(async () => ({ modules: [] })),
  getProfile: vi.fn(async () => ({
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
  })),
  updateProfile: vi.fn(async () => ({})),
  disconnectNextcloudToken: vi.fn(async () => ({})),
  // The Identity card's profile-picture control. The whole module is mocked,
  // so anything the page (or a primitive it mounts) imports has to be here or
  // the page throws on load and every assertion below reads as a missing row.
  uploadAvatar: vi.fn(async () => ({ hash: 'h1', mime: 'image/webp', bytes: 1 })),
  deleteAvatar: vi.fn(async () => ({ deleted: true })),
  avatarUrl: vi.fn(() => '/api/avatars/user/alice'),
  AVATAR_ACCEPT: 'image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif',
}));
vi.mock('$lib/api', () => api);

const native = vi.hoisted(() => ({
  isNativeShell: vi.fn(() => true),
  shellVersion: vi.fn(() => '0.10.0'),
  shellAtLeast: vi.fn(() => true),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));
vi.mock('$lib/platform/native', () => native);

const offline = vi.hoisted(() => ({
  clearOfflineData: vi.fn(async () => ({ workers: 1, caches: 1, database: true })),
}));
vi.mock('$lib/offline/clear', () => offline);

import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { User } from '$lib/api';

const ROW = /Clear offline data/;

// The page reads the identity the root layout resolved rather than fetching one
// (ISSUE-355), so the harness stands in for that layout. Nothing here turns on
// what is in the record.
const person: User = {
  username: 'alice',
  display_name: 'Alice',
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

const renderPage = () => render(Harness, { component: Page, user: person });

beforeEach(() => {
  native.shellAtLeast.mockReturnValue(true);
  offline.clearOfflineData.mockClear();
});

afterEach(() => {
  cleanup();
});

describe('the "Clear offline data" row', () => {
  it('is offered by a shell new enough to have a worker to clear', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByRole('button', { name: ROW })).toBeInTheDocument());
    expect(native.shellAtLeast).toHaveBeenCalledWith('0.10.0');
  });

  it('is absent in a browser and in an older app', async () => {
    native.shellAtLeast.mockReturnValue(false);
    renderPage();
    // Waited on something that does render, so this is an absence after the
    // page settled rather than before it started.
    await waitFor(() => expect(screen.getByText('Appearance')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: ROW })).toBeNull();
  });

  it('asks before it clears anything', async () => {
    renderPage();
    const button = await screen.findByRole('button', { name: ROW });
    button.click();

    await waitFor(() => expect(screen.getByText(/Are you sure/)).toBeInTheDocument());
    // The confirm gate is the whole point of the dialog: a mistap must not
    // take the app offline-unusable and reload the page under the user.
    expect(offline.clearOfflineData).not.toHaveBeenCalled();
  });

  it('says so, and does not reload, when the data is still there', async () => {
    // The row exists to escape a state the user can see. Reloading over a
    // clear that did nothing would present the failure as the fix, and take
    // the report away with it.
    offline.clearOfflineData.mockResolvedValueOnce({ workers: 0, caches: 0, database: false });
    const reload = vi.fn();
    Object.defineProperty(window, 'location', {
      value: { ...window.location, reload },
      configurable: true,
    });

    renderPage();
    (await screen.findByRole('button', { name: ROW })).click();
    (await screen.findByRole('button', { name: 'Clear' })).click();

    await waitFor(() => expect(screen.getByText(/Could not clear the offline data/)).toBeVisible());
    expect(reload).not.toHaveBeenCalled();
  });
});
