/**
 * Picking the room a `web` delivery route lands in (ISSUE-473).
 *
 * `web` has been selectable in the three routing dropdowns since ISSUE-121, but
 * only as a bare surface — the room it resolved to was the server's pick, and
 * nothing in the UI named it. These pin the two halves of the fix: the room
 * dropdown appears exactly when the surface is `web`, and what it writes is the
 * `web:<token>` descriptor the server has always accepted.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import type { User, UserProfile } from '$lib/api';

const GENERAL = 'web-alice-general';
const IDEAS = 'web-alice-ideas';

function profile(routing: Record<string, string> = {}): UserProfile {
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
    routing,
    default_destination: 'talk',
    briefing_email_html: true,
    timezone_follow_location: false,
    external_turn_display: 'collapsed',
    delivery_surfaces: ['talk', 'email', 'ntfy', 'web'],
    web_rooms: [
      { token: GENERAL, name: 'general', default: true, shared: false, channel: false },
      { token: IDEAS, name: 'ideas', default: false, shared: false, channel: false },
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

describe('the room a web route lands in', () => {
  it('offers no room picker while the surface is not web', async () => {
    currentProfile.value = profile({ alert: 'ntfy' });
    renderPage();
    await settled();
    expect(control('Alert delivery destination')).toBeTruthy();
    expect(control('Alert delivery room')).toBeNull();
  });

  it('offers one for each of the three routes once web is chosen', async () => {
    currentProfile.value = profile({ alert: 'web', log: 'web' });
    currentProfile.value.default_destination = 'web';
    renderPage();
    await settled();
    expect(control('Alert delivery room')).toBeTruthy();
    expect(control('Execution log room')).toBeTruthy();
    expect(control('Default delivery room')).toBeTruthy();
  });

  it('names the room a bare web route lands in, rather than leaving it unsaid', async () => {
    // The complaint the issue was filed on: `web` was selectable and the room
    // it resolved to appeared nowhere.
    currentProfile.value = profile({ alert: 'web' });
    renderPage();
    await settled();
    expect(control('Alert delivery room')).toHaveTextContent('Default room (general)');
  });

  it('shows the pinned room when the route already names one', async () => {
    currentProfile.value = profile({ alert: `web:${IDEAS}` });
    renderPage();
    await settled();
    // The surface dropdown reads `web`, not the whole descriptor — the room is
    // the other control's business.
    expect(control('Alert delivery destination')).toHaveTextContent('web');
    expect(control('Alert delivery room')).toHaveTextContent('ideas');
  });

  it('still shows a web log route somebody already set, rather than dropping it', async () => {
    // `routeOptions` keeps a value that is not among the offered surfaces, so an
    // operator-set route stays visible and editable instead of being silently
    // rewritten on the next save.
    currentProfile.value = profile({ log: `web:${IDEAS}` });
    renderPage();
    await settled();
    expect(control('Execution log destination')).toHaveTextContent('web');
    expect(control('Execution log room')).toHaveTextContent('ideas');
  });

  it('keeps a talk descriptor whole, since nothing here could put the token back', async () => {
    // A `talk:<token>` set from the CLI is offered back as its own option. If
    // the surface control split it the token would be dropped on the next save.
    currentProfile.value = profile({ alert: 'talk:9erk494s' });
    renderPage();
    await settled();
    expect(control('Alert delivery destination')).toHaveTextContent('talk:9erk494s');
    expect(control('Alert delivery room')).toBeNull();
  });
});
