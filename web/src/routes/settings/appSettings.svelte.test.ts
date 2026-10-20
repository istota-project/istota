/**
 * The settings row that opens the generic iOS app's own settings (0.12.0).
 *
 * It is a convenience beside the nav menu item and the app icon's quick action, so what
 * matters is that it appears only where the app can answer, that it names the
 * device, and that a refusal is reported rather than swallowed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
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
      default_room: '',
      delivery_surfaces: ['talk'],
    },
  })),
  updateProfile: vi.fn(async () => ({})),
  disconnectNextcloudToken: vi.fn(async () => ({})),
  // The Identity card's profile-picture control, which the page mounts.
  // `avatarUrl` is stubbed rather than left real so no `<img>` here points at
  // an address jsdom would try to fetch.
  uploadAvatar: vi.fn(async () => ({ hash: 'h1', mime: 'image/webp', bytes: 1 })),
  deleteAvatar: vi.fn(async () => ({ deleted: true })),
  avatarUrl: vi.fn(() => '/api/avatars/user/alice'),
});

const native = vi.hoisted(() => ({
  isNativeShell: vi.fn(() => true),
  shellVersion: vi.fn(() => '0.12.0'),
  shellAtLeast: vi.fn(() => true),
  shellRunsServiceWorker: vi.fn(() => false),
  deviceModel: vi.fn((): 'iPhone' | 'iPad' => 'iPhone'),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));
vi.mock('$lib/platform/native', () => native);

const app = vi.hoisted(() => ({
  appConnectionAvailable: vi.fn(() => true),
  openAppSettings: vi.fn(async () => true),
}));
vi.mock('$lib/platform/nativeConnection', () => app);

const notices = vi.hoisted(() => ({ notifyError: vi.fn() }));
vi.mock('$lib/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/stores/notices')>()),
  notifyError: notices.notifyError,
}));

import Page from './preferences/+page.svelte';
import SettingsLayout from './+layout.svelte';
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

const renderPage = () => render(Harness, { component: Page, layout: SettingsLayout, user: person });

const BUTTON = { name: 'Device settings' };

beforeEach(() => {
  vi.clearAllMocks();
  app.appConnectionAvailable.mockReturnValue(true);
  app.openAppSettings.mockResolvedValue(true);
  native.deviceModel.mockReturnValue('iPhone');
});

afterEach(() => {
  cleanup();
});

describe('the "Device settings" row', () => {
  it('opens the native settings in the generic app', async () => {
    renderPage();
    expect(await screen.findByText('iPhone')).toBeInTheDocument();
    expect(
      screen.getByText('Server, location tracking and notifications on this device.'),
    ).toBeInTheDocument();
    (await screen.findByRole('button', BUTTON)).click();
    await waitFor(() => expect(app.openAppSettings).toHaveBeenCalledOnce());
    expect(notices.notifyError).not.toHaveBeenCalled();
  });

  it('is titled iPad on an iPad', async () => {
    native.deviceModel.mockReturnValue('iPad');
    renderPage();
    expect(await screen.findByText('iPad')).toBeInTheDocument();
    expect(screen.queryByText('iPhone')).toBeNull();
  });

  it('is absent in a browser and in an older app', async () => {
    app.appConnectionAvailable.mockReturnValue(false);
    renderPage();
    await waitFor(() => expect(screen.getByText('Appearance')).toBeInTheDocument());
    expect(screen.queryByRole('button', BUTTON)).toBeNull();
  });

  it('reports an app that refused to open them', async () => {
    app.openAppSettings.mockRejectedValueOnce(new Error('The connection changed.'));
    renderPage();
    (await screen.findByRole('button', BUTTON)).click();
    await waitFor(() => expect(notices.notifyError).toHaveBeenCalledOnce());
  });
});
