/**
 * Fields the deployment writes on every converge render disabled.
 *
 * The profile PUT refuses an edit to a managed field with a 409, so a page
 * that left the control live would turn the old silent revert into an
 * unexplained failed save. The field says who owns it instead.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import type { User, UserProfile } from '$lib/api';

function profile(managed: string[]): UserProfile {
  return {
    managed,
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
    external_turn_display: 'collapsed',
    relay_delivery: '',
    relay_delivery_options: [],
    delivery_surfaces: ['talk', 'email', 'ntfy', 'web'],
    web_rooms: [],
  };
}

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const currentProfile = vi.hoisted(() => ({ value: null as UserProfile | null }));
await fillApiDouble(api, {
  getSettingsServices: vi.fn(async () => ({ services: [] })),
  getModules: vi.fn(async () => ({ modules: ['feeds', 'money'] })),
  getProfile: vi.fn(async () => ({ profile: currentProfile.value })),
  updateProfile: vi.fn(async () => ({})),
  avatarUrl: vi.fn(() => '/api/avatars/user/alice'),
});

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import AccountPage from './+page.svelte';
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

const BADGE = 'Set by your administrator';

afterEach(() => cleanup());
beforeEach(() => {
  currentProfile.value = profile([]);
});

describe('managed fields on /settings', () => {
  it('disables a managed field and badges it, and leaves the rest editable', async () => {
    currentProfile.value = profile(['email_addresses']);
    render(Harness, { component: AccountPage, layout: SettingsLayout, user: person });
    const emails = await screen.findByDisplayValue('alice@example.com');
    expect(emails).toBeDisabled();
    expect(screen.getAllByText(BADGE)).toHaveLength(1);
    expect(screen.getByDisplayValue('Alice')).not.toBeDisabled();
  });

  it('shows no badge when nothing is managed', async () => {
    render(Harness, { component: AccountPage, layout: SettingsLayout, user: person });
    const emails = await screen.findByDisplayValue('alice@example.com');
    expect(emails).not.toBeDisabled();
    expect(screen.queryByText(BADGE)).not.toBeInTheDocument();
  });

  it('disables managed lists and module toggles on Preferences', async () => {
    currentProfile.value = profile(['trusted_email_senders', 'disabled_modules']);
    render(Harness, { component: PreferencesPage, layout: SettingsLayout, user: person });
    await waitFor(() => expect(screen.getByLabelText('feeds')).toBeInTheDocument());
    expect(screen.getByLabelText('feeds')).toBeDisabled();
    expect(screen.getByLabelText('money')).toBeDisabled();
    expect(screen.getAllByText(BADGE)).toHaveLength(2);
    const quiet = screen.getByRole('textbox', { name: /Quiet email senders/ });
    expect(quiet).not.toBeDisabled();
  });
});
