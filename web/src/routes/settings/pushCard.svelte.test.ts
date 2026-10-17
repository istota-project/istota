import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { User, UserProfile, PushDevice } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import Preferences from './preferences/+page.svelte';
import SettingsLayout from './+layout.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

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
};

beforeEach(() => {
  vi.clearAllMocks();
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
  api.listPushDevices.mockResolvedValue({ devices: [] });
  api.deletePushDevice.mockResolvedValue({ ok: true });
  api.updateProfile.mockImplementation(async (patch) => {
    Object.assign(profile, patch);
    return {};
  });
});
afterEach(cleanup);

function mount() {
  return render(Harness, { user: person, component: Preferences, layout: SettingsLayout });
}

it('renders the browser stand-in and an empty device list', async () => {
  mount();
  expect(await screen.findByText('Register a device from the Istota iOS app.')).toBeInTheDocument();
  expect(await screen.findByText('No devices registered.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Register' })).toBeNull();
});

it('saves both preferences through the shared settings Save action', async () => {
  mount();
  const previews = await screen.findByRole('checkbox', { name: /Show message previews/ });
  expect(previews).toBeChecked();
  await fireEvent.click(previews);
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
    }),
  );
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Chat notifications' })).toHaveTextContent(
      'Badge only',
    ),
  );
  expect(screen.getByRole('checkbox', { name: /Show message previews/ })).not.toBeChecked();
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
