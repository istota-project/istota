/**
 * Preferences picks disabled skills from the list the server offers (ISSUE-793).
 *
 * The field used to be a comma-separated text input, so a typo was saved
 * without complaint. A stored name the list no longer offers stays picked, so
 * saving another change does not drop it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { User, UserProfile } from '$lib/api';

function profile(overrides: Partial<UserProfile> = {}): UserProfile {
  return {
    managed: [],
    user_id: 'alice',
    display_name: 'Alice',
    timezone: 'UTC',
    email_addresses: ['alice@example.com'],
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
    show_nav_unread_badges: false,
    external_turn_display: 'collapsed',
    relay_delivery: '',
    relay_delivery_options: [],
    delivery_surfaces: ['talk', 'email', 'ntfy', 'web'],
    web_rooms: [],
    ...overrides,
  };
}

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const currentProfile = vi.hoisted(() => ({ value: null as UserProfile | null }));
await fillApiDouble(api, {
  getSettingsServices: vi.fn(async () => ({ services: [] })),
  getModules: vi.fn(async () => ({
    modules: [],
    disabled: [],
    enabled_for_user: {},
    skills: ['browse', 'calendar', 'email'],
  })),
  getProfile: vi.fn(async () => ({ profile: currentProfile.value })),
  updateProfile: vi.fn(async () => ({})),
  avatarUrl: vi.fn(() => '/api/avatars/user/alice'),
});

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  shellRunsServiceWorker: vi.fn(() => false),
  deviceModel: vi.fn(() => 'iPhone'),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import PreferencesPage from './preferences/+page.svelte';
import SettingsLayout from './+layout.svelte';
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

afterEach(() => cleanup());
beforeEach(() => {
  api.updateProfile.mockClear();
  currentProfile.value = profile();
});

async function pick(query: string) {
  const search = await screen.findByRole('combobox', { name: 'Disabled skills' });
  await fireEvent.input(search, { target: { value: query } });
  await fireEvent.keyDown(search, { key: 'ArrowDown' });
  await fireEvent.keyDown(search, { key: 'Enter' });
}

describe('disabled skills on Preferences', () => {
  it('is a picker over the offered skills, not a text field', async () => {
    render(Harness, { component: PreferencesPage, layout: SettingsLayout, user: person });
    expect(await screen.findByRole('combobox', { name: 'Disabled skills' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /Disabled skills/ })).toBeNull();
  });

  it('turns a skill off by picking it and saves only that change', async () => {
    render(Harness, { component: PreferencesPage, layout: SettingsLayout, user: person });
    await pick('brow');
    await screen.findByRole('button', { name: 'Remove browse' });
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() =>
      expect(api.updateProfile).toHaveBeenCalledExactlyOnceWith({ disabled_skills: ['browse'] }),
    );
  });

  it('keeps a stored skill the list no longer offers when another is picked', async () => {
    currentProfile.value = profile({ disabled_skills: ['retired'] });
    render(Harness, { component: PreferencesPage, layout: SettingsLayout, user: person });
    await screen.findByRole('button', { name: 'Remove retired' });
    await pick('cal');
    await screen.findByRole('button', { name: 'Remove calendar' });
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() =>
      expect(api.updateProfile).toHaveBeenCalledExactlyOnceWith({
        disabled_skills: ['retired', 'calendar'],
      }),
    );
  });

  it('renders a stored list that repeats a name, and saves it once', async () => {
    // The old free-text field could store `email, email`; the chips are keyed by name.
    currentProfile.value = profile({ disabled_skills: ['email', 'email'] });
    render(Harness, { component: PreferencesPage, layout: SettingsLayout, user: person });
    expect(await screen.findAllByRole('button', { name: 'Remove email' })).toHaveLength(1);
    await pick('cal');
    await screen.findByRole('button', { name: 'Remove calendar' });
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() =>
      expect(api.updateProfile).toHaveBeenCalledExactlyOnceWith({
        disabled_skills: ['email', 'calendar'],
      }),
    );
  });

  it('shows a managed list as chips with no way to change it', async () => {
    currentProfile.value = profile({ managed: ['disabled_skills'], disabled_skills: ['email'] });
    render(Harness, { component: PreferencesPage, layout: SettingsLayout, user: person });
    expect(await screen.findByText('email')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove email' })).toBeNull();
    expect(screen.queryByRole('combobox', { name: 'Disabled skills' })).toBeNull();
    expect(screen.getByText('Set by your administrator')).toBeInTheDocument();
  });
});
