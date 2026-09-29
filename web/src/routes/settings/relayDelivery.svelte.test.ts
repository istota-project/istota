/**
 * Where relay questions from other users reach you.
 *
 * The server answers which destinations would work for this account, from the
 * relay resolver's own checks, and the page greys out the rest rather than
 * hiding them. The save sends only this field, so a stale tab cannot overwrite
 * anything else in the profile.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { RelayDelivery, RelayDeliveryOption, User, UserProfile } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';

function profile(
  relayDelivery: RelayDelivery = '',
  available: Partial<Record<RelayDelivery, boolean>> = {},
): UserProfile {
  const options: RelayDeliveryOption[] = (['', 'room', 'whatsapp', 'sms'] as RelayDelivery[]).map(
    (value) => ({ value, available: value === '' || (available[value] ?? false) }),
  );
  return {
    user_id: 'alice',
    display_name: 'Alice',
    timezone: 'UTC',
    email_addresses: [],
    trusted_email_senders: [],
    quiet_email_senders: [],
    log_channel: '',
    alerts_channel: '',
    disabled_skills: [],
    disabled_modules: [],
    max_foreground_workers: 0,
    max_background_workers: 0,
    routing: {},
    default_destination: 'talk',
    default_room: '',
    briefing_email_html: true,
    timezone_follow_location: false,
    external_turn_display: 'collapsed',
    relay_delivery: relayDelivery,
    relay_delivery_options: options,
    delivery_surfaces: ['talk', 'email', 'ntfy', 'web'],
    web_rooms: [],
  };
}

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const currentProfile = vi.hoisted(() => ({ value: null as UserProfile | null }));
await fillApiDouble(api, {
  getSettingsServices: vi.fn(async () => ({ services: [] })),
  getModules: vi.fn(async () => ({ modules: [] })),
  getProfile: vi.fn(async () => ({ profile: currentProfile.value })),
  updateProfile: vi.fn(async () => ({})),
  disconnectNextcloudToken: vi.fn(async () => ({})),
  uploadAvatar: vi.fn(async () => ({ hash: 'h1', mime: 'image/webp', bytes: 1 })),
  deleteAvatar: vi.fn(async () => ({ deleted: true })),
  avatarUrl: vi.fn(() => '/api/avatars/user/alice'),
});

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

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

const LABEL = 'Questions from other users';

async function mount() {
  render(Harness, { component: Page, user: person });
  await waitFor(() => expect(screen.getByText('Appearance')).toBeInTheDocument());
}

async function open() {
  const trigger = screen.getByRole('button', { name: LABEL });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
}

async function pick(optionLabel: string) {
  await open();
  const option = await screen.findByRole('option', { name: optionLabel });
  await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(option);
}

beforeEach(() => {
  currentProfile.value = profile();
  api.updateProfile.mockClear();
});

afterEach(() => {
  cleanup();
});

describe('questions from other users', () => {
  it("reads the asker's choice when nothing is set", async () => {
    await mount();
    expect(screen.getByRole('button', { name: LABEL })).toHaveTextContent(
      "Asker's choice (default room)",
    );
  });

  it('offers all four choices', async () => {
    currentProfile.value = profile('', { room: true, whatsapp: true, sms: true });
    await mount();
    await open();
    for (const name of ["Asker's choice (default room)", 'My default room', 'WhatsApp', 'SMS']) {
      expect(await screen.findByRole('option', { name })).toBeTruthy();
    }
  });

  it('disables what is not set up for this account, and says which', async () => {
    currentProfile.value = profile('', { room: true });
    await mount();
    expect(screen.getByText('Not set up for your account: WhatsApp, SMS.')).toBeInTheDocument();
    await open();
    const whatsapp = await screen.findByRole('option', { name: 'WhatsApp (not set up)' });
    const sms = await screen.findByRole('option', { name: 'SMS (not set up)' });
    const room = await screen.findByRole('option', { name: 'My default room' });
    expect(whatsapp).toHaveAttribute('data-disabled');
    expect(sms).toHaveAttribute('data-disabled');
    expect(room).not.toHaveAttribute('data-disabled');
  });

  it('keeps a saved preference that has stopped working visible and selected', async () => {
    // The resolver falls back to the default room for it, but the row still
    // holds the value, so hiding it would misstate what is stored.
    currentProfile.value = profile('whatsapp', { room: true });
    await mount();
    expect(screen.getByRole('button', { name: LABEL })).toHaveTextContent('WhatsApp (not set up)');
  });

  it('saves only the changed field', async () => {
    currentProfile.value = profile('', { room: true, sms: true });
    await mount();
    await pick('SMS');
    await waitFor(() => expect(get(settingsSave)?.dirty).toBe(true));
    await get(settingsSave)!.save();
    await waitFor(() => expect(api.updateProfile).toHaveBeenCalledOnce());
    expect(api.updateProfile).toHaveBeenCalledWith({ relay_delivery: 'sms' });
  });
});
