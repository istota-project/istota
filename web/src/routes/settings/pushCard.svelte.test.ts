import { tick } from 'svelte';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { User, UserProfile, PushDevice } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import Preferences from './preferences/+page.svelte';
import Notifications from './notifications/+page.svelte';
import { page } from '$app/state';
import { base } from '$app/paths';
import SettingsLayout from './+layout.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import { startNotificationPoll, resetNotifications } from '$lib/stores/notifications';

const person = {
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
} satisfies User;
let profile: UserProfile;
const device: PushDevice = {
  id: 7,
  platform: 'ios',
  app_version: '0.11.0',
  environment: 'sandbox',
  token_tail: 'ababab',
  created_at: '2026-01-01T00:00:00Z',
  last_seen_at: null,
  last_error: null,
  content: 'legacy',
  stale_key: false,
  enrollment_state: 'active',
};

beforeEach(() => {
  vi.clearAllMocks();
  page.url = new URL(`http://localhost${base}/settings/notifications`) as typeof page.url;
  Object.defineProperty(navigator, 'userAgent', { value: 'Mozilla/5.0', configurable: true });
  profile = {
    user_id: 'alice',
    display_name: 'Alice',
    email_addresses: [],
    timezone: 'UTC',
    log_channel: '',
    alerts_channel: '',
    max_foreground_workers: 0,
    max_background_workers: 0,
    disabled_skills: [],
    trusted_email_senders: [],
    quiet_email_senders: [],
    disabled_modules: [],
    default_destination: 'talk',
    default_room: '',
    routing: {},
    external_turn_display: 'collapsed',
    relay_delivery: '',
    briefing_email_html: true,
    timezone_follow_location: false,
    show_nav_unread_badges: false,
    push_chat_alerts: 'alerts',
    push_preview: true,
  };
  api.getProfile.mockImplementation(async () => ({ profile: structuredClone(profile) }));
  api.getModules.mockResolvedValue({ modules: [] });
  api.listPushCategories.mockResolvedValue({
    categories: [
      { id: 'confirmation', label: 'Approvals' },
      { id: 'new_source', label: 'A new source from the server' },
    ],
  });
  api.listPushDevices.mockResolvedValue({ devices: [] });
  api.deletePushDevice.mockResolvedValue({ ok: true });
  api.updateProfile.mockImplementation(async (patch) => {
    Object.assign(profile, JSON.parse(JSON.stringify(patch)));
    return {};
  });
});
afterEach(() => {
  cleanup();
  resetNotifications();
  vi.unstubAllGlobals();
});

function mount() {
  return render(Harness, { user: person, component: Notifications, layout: SettingsLayout });
}

it('links to a dedicated Notifications section', async () => {
  mount();
  expect(await screen.findByRole('link', { name: 'Notifications' })).toHaveAttribute(
    'href',
    `${base}/settings/notifications`,
  );
});

it.each(['Mozilla/5.0', 'Mozilla/5.0 IstotaApp/0.10.0'])(
  'renders preferences and the device stand-in for %s',
  async (userAgent) => {
    Object.defineProperty(navigator, 'userAgent', { value: userAgent, configurable: true });
    mount();
    expect(
      await screen.findByText('Register a device from the Istota iOS app.'),
    ).toBeInTheDocument();
    expect(await screen.findByText('No devices registered.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Register/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Chat notifications' })).toBeEnabled();
    expect(await screen.findByRole('checkbox', { name: 'Approvals' })).toBeEnabled();
  },
);

it('saves categories and both preferences through the shared settings Save action', async () => {
  api.getNotificationCounts.mockResolvedValue({ open: 4, actionable: 1, push_badge: 2 });
  startNotificationPoll();
  mount();
  const previews = await screen.findByRole('checkbox', { name: /Show message previews/ });
  expect(previews).toBeChecked();
  await fireEvent.click(previews);
  await fireEvent.click(await screen.findByRole('checkbox', { name: 'Approvals' }));
  await fireEvent.pointerDown(screen.getByRole('button', { name: 'Chat notifications' }), {
    pointerType: 'mouse',
    button: 0,
  });
  const option = await screen.findByRole('option', { name: 'Badge only' });
  await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(option);
  expect(api.updateProfile).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(api.updateProfile).toHaveBeenCalledWith({
      push_chat_alerts: 'badge',
      push_preview: false,
      push_inbox_categories: { confirmation: false },
    }),
  );
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Chat notifications' })).toHaveTextContent(
      'Badge only',
    ),
  );
  expect(screen.getByRole('checkbox', { name: /Show message previews/ })).not.toBeChecked();
  expect(api.getNotificationCounts).toHaveBeenCalledTimes(2);
});

it('removes a listed device immediately through its own API', async () => {
  api.listPushDevices.mockResolvedValue({ devices: [device] });
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Remove device ending ababab' }));
  expect(await screen.findByText('No devices registered.')).toBeInTheDocument();
  expect(api.deletePushDevice).toHaveBeenCalledWith(7);
  expect(api.updateProfile).not.toHaveBeenCalled();
});

it('keeps the row and reports a failed removal', async () => {
  api.listPushDevices.mockResolvedValue({ devices: [device] });
  api.deletePushDevice.mockRejectedValueOnce(new Error('Removal failed'));
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Remove device ending ababab' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Removal failed');
  expect(screen.getByRole('button', { name: 'Remove device ending ababab' })).toBeEnabled();
});

it('uses server labels and enables missing category keys without dirtying the profile', async () => {
  const realApi = await vi.importActual<typeof import('$lib/api')>('$lib/api');
  const fetch = vi.fn(async () =>
    Response.json({
      categories: [
        { id: 'confirmation', label: 'Approvals' },
        { id: 'new_source', label: 'A new source from the server' },
      ],
    }),
  );
  vi.stubGlobal('fetch', fetch);
  api.listPushCategories.mockImplementationOnce(realApi.listPushCategories);
  profile.push_inbox_categories = { confirmation: false };
  mount();
  expect(await screen.findByRole('checkbox', { name: 'Approvals' })).not.toBeChecked();
  expect(screen.getByRole('checkbox', { name: 'A new source from the server' })).toBeChecked();
  expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  expect(api.getProfile).toHaveBeenCalledTimes(1);
  expect(fetch).toHaveBeenCalledWith(`${base}/api/push/categories`, expect.any(Object));
});

it('keeps edits across sections and saves them together', async () => {
  const view = mount();
  await fireEvent.click(await screen.findByRole('checkbox', { name: 'Approvals' }));
  expect(screen.getByRole('link', { name: 'Notifications' })).toHaveAttribute(
    'aria-current',
    'page',
  );
  page.url = new URL(`http://localhost${base}/settings/preferences`) as typeof page.url;
  await view.rerender({ user: person, component: Preferences, layout: SettingsLayout });
  await fireEvent.click(
    await screen.findByRole('checkbox', { name: /Show unread counts in navigation/ }),
  );
  expect(screen.queryByRole('heading', { name: 'Push notifications' })).toBeNull();
  page.url = new URL(`http://localhost${base}/settings/notifications`) as typeof page.url;
  await view.rerender({ user: person, component: Notifications, layout: SettingsLayout });
  expect(await screen.findByRole('checkbox', { name: 'Approvals' })).not.toBeChecked();
  expect(api.getProfile).toHaveBeenCalledTimes(1);
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(api.updateProfile).toHaveBeenCalledExactlyOnceWith({
      show_nav_unread_badges: true,
      push_inbox_categories: { confirmation: false },
    }),
  );
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled());
});

it('can re-enable a stored disabled category', async () => {
  profile.push_inbox_categories = { confirmation: false };
  mount();
  await fireEvent.click(await screen.findByRole('checkbox', { name: 'Approvals' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(api.updateProfile).toHaveBeenCalledExactlyOnceWith({
      push_inbox_categories: { confirmation: true },
    }),
  );
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled());
  expect(screen.getByRole('checkbox', { name: 'Approvals' })).toBeChecked();
});

it('keeps category edits after a failed save so they can be retried', async () => {
  api.updateProfile.mockRejectedValueOnce(new Error('Save refused'));
  mount();
  await fireEvent.click(await screen.findByRole('checkbox', { name: 'Approvals' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  expect(await screen.findByText('Save refused')).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: 'Approvals' })).not.toBeChecked();
  expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled();
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled());
  expect(profile.push_inbox_categories).toEqual({ confirmation: false });
});

it('reports category loading failure while leaving devices and preferences usable', async () => {
  api.listPushCategories.mockRejectedValueOnce(new Error('Categories unavailable'));
  mount();
  expect(await screen.findByRole('alert')).toHaveTextContent('Categories unavailable');
  expect(await screen.findByText('No devices registered.')).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: /Show message previews/ })).toBeEnabled();
  await fireEvent.click(screen.getByRole('button', { name: 'Retry categories' }));
  expect(await screen.findByRole('checkbox', { name: 'Approvals' })).toBeChecked();
  expect(screen.queryByRole('alert')).toBeNull();
});

it('respects managed category and chat preferences', async () => {
  profile.managed = ['push_inbox_categories', 'push_chat_alerts', 'push_preview'];
  mount();
  expect(await screen.findByRole('checkbox', { name: /Approvals/ })).toBeDisabled();
  expect(screen.getByRole('checkbox', { name: /A new source from the server/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Chat notifications' })).toBeDisabled();
  expect(screen.getByRole('checkbox', { name: /Show message previews/ })).toBeDisabled();
});

function nativePush(permission = 'prompt') {
  Object.defineProperty(navigator, 'userAgent', { value: 'IstotaApp/0.11.0', configurable: true });
  const plugin = {
    permissions: vi.fn(async () => ({ permission, registered: false })),
    register: vi.fn(async () => ({
      permission: 'granted',
      token: 'ab'.repeat(32),
      environment: 'sandbox',
    })),
    setBadge: vi.fn(async () => {}),
  };
  const openAppSettings = vi.fn(async () => {});
  vi.stubGlobal('Capacitor', {
    Plugins: { IstotaPush: plugin, IstotaLocation: { openAppSettings } },
  });
  return { plugin, openAppSettings };
}

it('registers a Debug token through the real cookie-authenticated API client', async () => {
  const { plugin } = nativePush();
  const real = await vi.importActual<typeof import('$lib/api')>('$lib/api');
  api.registerPushDevice.mockImplementation(real.registerPushDevice);
  const request = vi.fn(async () => new Response(JSON.stringify({ id: 7 })));
  vi.stubGlobal('fetch', request);
  mount();
  const button = await screen.findByRole('button', { name: 'Register this device' });
  expect(plugin.register).not.toHaveBeenCalled();
  await fireEvent.click(button);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  const [url, options] = request.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe(`${base}/api/push/devices`);
  expect(options.method).toBe('POST');
  expect(options.credentials).toBe('same-origin');
  expect(JSON.parse(options.body as string)).toEqual({
    token: 'ab'.repeat(32),
    environment: 'sandbox',
    platform: 'ios',
    app_version: '0.11.0',
  });
  await waitFor(() => expect(api.listPushDevices).toHaveBeenCalledTimes(2));
});
it('reports denied authorization and opens the existing app Settings control', async () => {
  const { plugin, openAppSettings } = nativePush();
  plugin.register.mockResolvedValue({
    permission: 'denied',
    token: null as unknown as string,
    environment: 'sandbox',
  });
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  expect(await screen.findByText(/Notifications are denied/)).toBeInTheDocument();
  expect(api.registerPushDevice).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Open iOS Settings' }));
  expect(openAppSettings).toHaveBeenCalledTimes(1);
});
it('reports registration failures without claiming success', async () => {
  const { plugin } = nativePush();
  plugin.register.mockRejectedValue(new Error('Token request timed out.'));
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Token request timed out.');
  expect(api.registerPushDevice).not.toHaveBeenCalled();
});

it('keeps a denial from registration when an earlier permission read finishes late', async () => {
  const { plugin } = nativePush();
  let resolve!: (value: { permission: string; registered: boolean }) => void;
  plugin.permissions.mockImplementationOnce(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  plugin.register.mockResolvedValue({
    permission: 'denied',
    token: null as unknown as string,
    environment: 'sandbox',
  });
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  await screen.findByText(/Notifications are denied/);
  resolve({ permission: 'prompt', registered: false });
  await Promise.resolve();
  await Promise.resolve();
  await tick();
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Open iOS Settings' })).toBeInTheDocument(),
  );
});

it('keeps registration retryable when enrollment fails on the server', async () => {
  nativePush();
  api.registerPushDevice.mockRejectedValueOnce(new Error('Enrollment failed.'));
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Enrollment failed.');
  expect(screen.queryByText('This device is registered.')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Register this device' })).toBeEnabled();
});

const binding = '0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51';
function genericShell() {
  const { plugin } = nativePush();
  Object.defineProperty(navigator, 'userAgent', { value: 'IstotaApp/0.12.0', configurable: true });
  const connection = {
    current: vi.fn(async () => ({
      connectionBinding: binding,
      generation: 3,
      baseURL: 'https://your-host.tld/istota/',
      state: 'active',
    })),
    recordPushRegistration: vi.fn(async () => {}),
  };
  const capacitor = globalThis as unknown as { Capacitor: { Plugins: Record<string, unknown> } };
  capacitor.Capacitor.Plugins.IstotaConnection = connection;
  return { plugin, connection };
}

it('binds a generic-shell enrollment to the native connection and hands back its ID', async () => {
  const { connection } = genericShell();
  api.registerPushDevice.mockResolvedValue({ id: 41 });
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  expect(await screen.findByText('This device is registered.')).toBeInTheDocument();
  expect(api.registerPushDevice).toHaveBeenCalledWith({
    token: 'ab'.repeat(32),
    environment: 'sandbox',
    platform: 'ios',
    app_version: '0.12.0',
    connection_binding: binding,
    connection_generation: 3,
  });
  expect(connection.recordPushRegistration).toHaveBeenCalledWith({
    registrationId: 41,
    connectionBinding: binding,
    generation: 3,
  });
});

it('does not prompt or enroll when the page no longer owns the connection', async () => {
  const { plugin, connection } = genericShell();
  connection.current.mockRejectedValueOnce(new Error('The connection changed.'));
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('The connection changed.');
  expect(plugin.register).not.toHaveBeenCalled();
  expect(api.registerPushDevice).not.toHaveBeenCalled();
});

function rowFor(tail: string) {
  return within(screen.getByText(new RegExp(`…${tail}$`)).closest('li') as HTMLElement);
}

it('names each device’s content mode and says where private previews are opened', async () => {
  api.listPushDevices.mockResolvedValue({
    devices: [
      { ...device, id: 1, token_tail: '000001', content: 'encrypted' },
      { ...device, id: 2, token_tail: '000002', content: 'generic' },
      { ...device, id: 3, token_tail: '000003', content: 'legacy' },
    ],
  });
  mount();
  await screen.findByText(/…000001$/);
  rowFor('000001').getByText('Private previews');
  rowFor('000002').getByText('Private, no preview');
  rowFor('000003').getByText('Standard previews');
  expect(screen.getByText(/decrypted on the phone/)).toBeInTheDocument();
  expect(screen.queryByText(/Register this device again/)).toBeNull();
});

it('asks a device enrolled under an earlier sender key to register again', async () => {
  api.listPushDevices.mockResolvedValue({
    devices: [{ ...device, content: 'generic', stale_key: true }],
  });
  mount();
  await screen.findByText(/…ababab$/);
  rowFor('ababab').getByText('Private, no preview');
  rowFor('ababab').getByText(/Register this device again/);
  expect(screen.queryByText(/Standard previews send message text/)).toBeNull();
});

it('leaves out the private explanation when every device is standard', async () => {
  api.listPushDevices.mockResolvedValue({ devices: [device] });
  mount();
  await screen.findByText(/…ababab$/);
  rowFor('ababab').getByText('Standard previews');
  expect(screen.queryByText(/decrypted on the phone/)).toBeNull();
  expect(screen.getByText(/Standard previews send message text/)).toBeInTheDocument();
});

it('enrolls a 0.15.0 app privately when the server offers it', async () => {
  const { plugin, connection } = genericShell();
  Object.defineProperty(navigator, 'userAgent', { value: 'IstotaApp/0.15.0', configurable: true });
  const native = plugin as unknown as Record<string, ReturnType<typeof vi.fn>>;
  native.preparePrivateRegistration = vi.fn(async () => ({
    content: 'generic',
    generation: 'G'.repeat(22),
    token: 'cd'.repeat(32),
    environment: 'production',
    versions: [1],
  }));
  native.commitPrivateRegistration = vi.fn(async () => ({}));
  native.cancelPrivateRegistration = vi.fn(async () => ({}));
  api.getMobilePushCapabilities.mockResolvedValue({
    delivery: 'direct',
    private: { versions: [1] },
  });
  api.getPushSenderKey.mockResolvedValue({ version: 1, key_id: 'S'.repeat(43), public_key: 'P' });
  api.registerPushDevice.mockResolvedValue({ id: 41 });
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  expect(await screen.findByText('This device is registered.')).toBeInTheDocument();
  expect(api.registerPushDevice).toHaveBeenCalledWith({
    token: 'cd'.repeat(32),
    environment: 'production',
    platform: 'ios',
    app_version: '0.15.0',
    connection_binding: binding,
    connection_generation: 3,
    private: { version: 1, content: 'generic', generation: 'G'.repeat(22) },
  });
  expect(native.commitPrivateRegistration).toHaveBeenCalledWith({
    binding,
    generation: 'G'.repeat(22),
    deviceId: 41,
  });
  expect(connection.recordPushRegistration).toHaveBeenCalledOnce();
  expect(native.cancelPrivateRegistration).not.toHaveBeenCalled();
});

it('keeps a lasting line on a device whose relay route needs repair', async () => {
  api.listPushDevices.mockResolvedValue({
    devices: [
      { ...device, id: 1, token_tail: '000001', content: 'encrypted', enrollment_state: 'repair' },
      { ...device, id: 2, token_tail: '000002', content: 'encrypted' },
    ],
  });
  mount();
  await screen.findByText(/…000001$/);
  expect(
    rowFor('000001').getByText(/Notifications to this device have stopped/),
  ).toBeInTheDocument();
  expect(rowFor('000001').getByText(/register it again/)).toBeInTheDocument();
  expect(rowFor('000002').queryByText(/have stopped/)).toBeNull();
});

it('shows a stale key under relay delivery as a stopped device, not a preview change', async () => {
  // The server lists a stale relay enrollment as repair: nothing reaches it until it enrolls again.
  api.listPushDevices.mockResolvedValue({
    devices: [{ ...device, content: 'generic', stale_key: true, enrollment_state: 'repair' }],
  });
  mount();
  await screen.findByText(/…ababab$/);
  rowFor('ababab').getByText(/Notifications to this device have stopped/);
  expect(rowFor('ababab').queryByText(/to show private previews/)).toBeNull();
});

it('says a relay installation needs iOS 17 when this device cannot enroll privately', async () => {
  const { plugin } = genericShell();
  Object.defineProperty(navigator, 'userAgent', { value: 'IstotaApp/0.16.0', configurable: true });
  const native = plugin as unknown as Record<string, ReturnType<typeof vi.fn>>;
  native.preparePrivateRegistration = vi.fn(async () => ({
    content: 'generic',
    generation: 'G'.repeat(22),
    token: 'cd'.repeat(32),
    environment: 'production',
    versions: [1],
  }));
  native.commitPrivateRegistration = vi.fn(async () => ({}));
  native.cancelPrivateRegistration = vi.fn(async () => ({}));
  api.getMobilePushCapabilities.mockResolvedValue({
    delivery: 'unavailable',
    private: { versions: [1] },
    relay: { versions: [1], url: 'https://relay.example.com/istota' },
  });
  api.getPushSenderKey.mockResolvedValue({ version: 1, key_id: 'S'.repeat(43), public_key: 'P' });
  api.registerPushDevice.mockRejectedValueOnce(
    Object.assign(new Error('private push not offered'), { status: 409 }),
  );
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Private notifications on this server need iOS 17 or later.',
  );
  expect(screen.queryByText('This device is registered.')).not.toBeInTheDocument();
  expect(native.cancelPrivateRegistration).toHaveBeenCalledOnce();
});

it('does not claim success when the app refuses the registration receipt', async () => {
  const { connection } = genericShell();
  api.registerPushDevice.mockResolvedValue({ id: 41 });
  connection.recordPushRegistration.mockRejectedValueOnce(new Error('The connection changed.'));
  mount();
  await fireEvent.click(await screen.findByRole('button', { name: 'Register this device' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('The connection changed.');
  expect(screen.queryByText('This device is registered.')).not.toBeInTheDocument();
});
