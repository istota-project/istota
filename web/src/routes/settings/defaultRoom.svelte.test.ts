/**
 * The default room picker (ISSUE-477).
 *
 * A destination naming no room — a bare `web` or a bare `talk` — had to land
 * somewhere, and nothing the user set decided where: web guessed at their
 * oldest private room, and the guess moved when they archived it. This is the
 * control that makes it a setting.
 *
 * It sits inside the `default_destination` row and opens only when that
 * transport has rooms, the same shape the alert and log rows use. It still
 * writes `default_room` rather than a room on the descriptor: that value names
 * a transport and nothing else (ISSUE-475), and the pin governs every bare
 * `web` and `talk` destination on both surfaces at once.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import type { User, UserProfile } from '$lib/api';

const GENERAL = 'web-alice-general';
const IDEAS = 'web-alice-ideas';

function profile(defaultRoom = ''): UserProfile {
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
    default_room: defaultRoom,
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

describe('the default room', () => {
  it('opens beside the transport when the transport has rooms', async () => {
    renderPage();
    await settled();
    expect(control('Default room')).toBeTruthy();
    // `default_destination` still names a transport and nothing else
    // (ISSUE-475), so this control did not reintroduce a room on that row.
    expect(control('Default delivery destination')).toBeTruthy();
    expect(control('Default delivery room')).toBeNull();
  });

  it('opens on web too, not just talk', async () => {
    currentProfile.value = profile(IDEAS);
    currentProfile.value.default_destination = 'web';
    renderPage();
    await settled();
    expect(control('Default room')).toHaveTextContent('ideas');
  });

  for (const surface of ['email', 'ntfy']) {
    it(`stays shut on ${surface}, which has no room to pick`, async () => {
      // The row it used to be stood below every transport, asking email and
      // ntfy users to read past a room they had no delivery landing in.
      currentProfile.value = profile(IDEAS);
      currentProfile.value.default_destination = surface;
      renderPage();
      await settled();
      expect(control('Default delivery destination')).toHaveTextContent(surface);
      expect(control('Default room')).toBeNull();
    });
  }

  it('reads "Automatic" when nothing is pinned', async () => {
    // Not "Default room (general)" — the web picker's leading option names the
    // room a bare `web` lands in, and here that room is whatever this control
    // says, so naming it would be circular.
    renderPage();
    await settled();
    expect(control('Default room')).toHaveTextContent('Automatic');
  });

  it('shows the pinned room when one is set', async () => {
    currentProfile.value = profile(IDEAS);
    renderPage();
    await settled();
    expect(control('Default room')).toHaveTextContent('ideas');
  });

  it('keeps an operator-set room the list does not carry', async () => {
    // Same reason `routeOptions` keeps a withdrawn surface: a room pinned from
    // the CLI, or one since archived, must stay visible and editable rather
    // than rendering blank and being cleared by the next save.
    currentProfile.value = profile('room-set-by-operator');
    renderPage();
    await settled();
    expect(control('Default room')).toHaveTextContent('room-set-by-operator');
  });

  it('offers a shared room, marked, rather than withholding it', async () => {
    // The exclusions on the heuristic are about keeping a *guess* out of a room
    // somebody else reads. Pinning one deliberately is allowed, and the mark is
    // what makes it an informed choice.
    currentProfile.value = profile();
    currentProfile.value.web_rooms = [
      { token: GENERAL, name: 'general', default: true, shared: false, channel: false },
      { token: 'shared-1', name: 'team', default: false, shared: true, channel: false },
    ];
    renderPage();
    await settled();
    expect(control('Default room')).toBeTruthy();
    expect(screen.getByLabelText('Default room')).toBeTruthy();
  });

  it('says so when the server reports the pin is being ignored', async () => {
    // ISSUE-479. Until now a dead `default_room` rendered as a bare token with
    // no mark, so a setting that had silently stopped working looked like one
    // pinned from the CLI. It is `(ignored)` rather than the route rows'
    // `(unavailable)` because the delivery still arrives — just not here.
    currentProfile.value = profile('room-archived');
    currentProfile.value.ignored_default_room = 'room-archived';
    renderPage();
    await settled();
    expect(control('Default room')).toHaveTextContent('room-archived (ignored)');
  });

  it('leaves an operator-set room the server did not name unmarked', async () => {
    // The control. A pin absent from `web_rooms` is not evidence of anything —
    // it may have no handle yet, or merely be hidden, both of which deliver. So
    // the server names a *different* token here: marking has to key on which
    // room was named, not on the key being present.
    currentProfile.value = profile('room-set-by-operator');
    currentProfile.value.ignored_default_room = 'room-archived-elsewhere';
    renderPage();
    await settled();
    expect(control('Default room')).toHaveTextContent('room-set-by-operator');
    expect(control('Default room')).not.toHaveTextContent('ignored');
  });
});
