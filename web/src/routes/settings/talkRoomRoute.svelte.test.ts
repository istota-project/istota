/**
 * Picking the Talk conversation an alert or log route lands in (ISSUE-475).
 *
 * `talk:<token>` has been a valid descriptor everywhere and was offered nowhere:
 * the old `talk (alerts channel)` / `talk (logs channel)` labels were not picks,
 * they were the bare `talk` value labelled with where it resolves for that
 * purpose. Wanting alerts in a different conversation was CLI-or-config.toml
 * only, which is the gap ISSUE-473 closed for web and left open on the surface
 * that has more rooms in it. Those labels are gone with the arrival of a control
 * that can say the same thing and act on it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import type { User, UserProfile } from '$lib/api';

const ALERTS = 'alerts-tok';
const TEAM = 'conv-team';

function profile(routing: Record<string, string> = {}): UserProfile {
  return {
    user_id: 'alice',
    display_name: 'Alice',
    timezone: 'UTC',
    email_addresses: [],
    trusted_email_senders: [],
    quiet_email_senders: [],
    log_channel: '',
    alerts_channel: ALERTS,
    disabled_skills: [],
    disabled_modules: [],
    max_foreground_workers: 0,
    max_background_workers: 0,
    routing,
    default_destination: 'talk',
    default_room: '',
    briefing_email_html: true,
    timezone_follow_location: false,
    external_turn_display: 'collapsed',
    delivery_surfaces: ['talk', 'email', 'ntfy', 'web'],
    web_rooms: [],
    talk_rooms: [
      { token: ALERTS, name: 'Alerts channel', channel: true },
      { token: TEAM, name: 'team', channel: false },
    ],
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

const renderPage = () => render(Harness, { component: Page, user: person });

/** The bits-ui trigger renders as a button carrying the control's aria-label. */
const control = (label: string) => screen.queryByRole('button', { name: label });

async function settled() {
  await waitFor(() => expect(screen.getByText('Appearance')).toBeInTheDocument());
}

beforeEach(() => {
  currentProfile.value = profile();
});

afterEach(() => {
  cleanup();
});

describe('the conversation a talk route lands in', () => {
  it('offers a room picker on the alert route', async () => {
    currentProfile.value = profile({ alert: 'talk' });
    renderPage();
    await settled();
    expect(control('Alert delivery room')).toBeTruthy();
  });

  it('offers one on the execution log too, which is where talk is the practical case', async () => {
    // `web` is deliberately withheld from the log dropdown, so Talk is the only
    // roomed surface this row can reach through the surface control.
    currentProfile.value = profile({ log: 'talk' });
    renderPage();
    await settled();
    expect(control('Execution log room')).toBeTruthy();
  });

  it('offers none on a surface that carries no room', async () => {
    currentProfile.value = profile({ alert: 'ntfy' });
    renderPage();
    await settled();
    expect(control('Alert delivery room')).toBeNull();
  });

  it('offers none on the default destination, which names a transport only', async () => {
    currentProfile.value = profile();
    currentProfile.value.default_destination = 'talk';
    renderPage();
    await settled();
    expect(control('Default delivery room')).toBeNull();
  });

  it('shows the pinned conversation when the route already names one', async () => {
    // Before this the whole `talk:<token>` sat in the surface dropdown as a raw
    // descriptor, because nothing could put the token back if it were split.
    currentProfile.value = profile({ alert: `talk:${TEAM}` });
    renderPage();
    await settled();
    expect(control('Alert delivery destination')).toHaveTextContent('talk');
    expect(control('Alert delivery room')).toHaveTextContent('team');
  });

  it('says where a bare talk lands rather than leaving the room unsaid', async () => {
    currentProfile.value = profile({ alert: 'talk' });
    renderPage();
    await settled();
    expect(control('Alert delivery room')).toHaveTextContent('Alerts channel (default)');
  });

  it('says it in the room picker only, not twice across both selects', async () => {
    // The surface label used to carry the same sentence, from before there was
    // a room control that could (ISSUE-475).
    currentProfile.value = profile({ alert: 'talk', log: 'talk' });
    renderPage();
    await settled();
    expect(control('Alert delivery destination')).toHaveTextContent(/^talk$/);
    expect(control('Execution log destination')).toHaveTextContent(/^talk$/);
    expect(control('Execution log room')).toHaveTextContent('Logs channel (default)');
  });

  it('marks a conversation the bot provisioned, as the web picker marks its own', async () => {
    currentProfile.value = profile({ alert: `talk:${ALERTS}` });
    renderPage();
    await settled();
    expect(control('Alert delivery room')).toHaveTextContent("bot's own channel");
  });

  it('keeps a conversation the server did not offer, rather than dropping it', async () => {
    // An operator-set token for a conversation the room registry has not seen.
    currentProfile.value = profile({ alert: 'talk:conv-old' });
    renderPage();
    await settled();
    expect(control('Alert delivery room')).toHaveTextContent('conv-old');
  });
});
