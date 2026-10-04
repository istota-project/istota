/**
 * Email thread rooms are kept out of the room list (hidden-email-threads,
 * stage 6).
 *
 * The listing still carries every room, because a deep link, the note's `re:`
 * chip and the room stream open a room by finding it there. The sidebar files
 * a thread the viewer has not listed under a collapsed "Email threads" group,
 * and its unread count does not reach the Unread total: the note in the
 * private room is the signal.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, screen } from '@testing-library/svelte';

vi.mock('$lib/stores/chat', async () => {
  const { writable } = await import('svelte/store');
  const stores: Record<string, unknown> = {
    rooms: writable([]),
    activeRoomId: writable(1),
    messages: writable([]),
    status: writable('idle'),
    loaded: writable(true),
    hasMore: writable(false),
    loadingOlder: writable(false),
    view: writable('room'),
    scrollTarget: writable(null),
    sendSettled: writable({ n: 0, token: null }),
    sendReturned: writable({ n: 0, token: null, text: '', attachments: [] }),
    outboundDrafts: writable([]),
    externalTurnDisplay: writable('full'),
    offlineTranscript: writable(false),
    queuedCounts: writable({}),
  };
  const session = new Proxy(stores, {
    get: (target, key: string) => (target[key] ??= vi.fn(async () => undefined)),
  });
  return { getChatSession: () => session };
});

import { getChatSession } from '$lib/stores/chat';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { User } from '$lib/api';

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

type Session = Record<string, { set: (v: unknown) => void }> & {
  updateRoomSettings: ReturnType<typeof vi.fn>;
};
const session = () => getChatSession() as unknown as Session;

const room = (id: number, over: Record<string, unknown> = {}) => ({
  id,
  token: `web-${id}`,
  name: `Room ${id}`,
  origin: 'web',
  talk_token: null,
  model: null,
  effort: null,
  brain: null,
  unread_count: 0,
  last_activity: new Date(Date.UTC(2026, 9, 4, 12, 0, 60 - id)).toISOString(),
  ...over,
});

const thread = (id: number, over: Record<string, unknown> = {}) =>
  room(id, { origin: 'email', read_only: true, email_thread: true, listed: false, ...over });

const renderPage = () => render(Harness, { component: Page, user: person });
const mainNames = () =>
  [...document.querySelectorAll('.room-list-main .room-name')].map((e) => e.textContent?.trim());
const groupNames = () =>
  [...document.querySelectorAll('.room-list-threads .room-name')].map((e) => e.textContent?.trim());
const groupToggle = () => screen.getByRole('button', { name: /Email threads/ });
const unreadPill = () =>
  [...document.querySelectorAll('.view-btn')]
    .find((b) => b.textContent?.includes('Unread'))!
    .querySelector('[title$="unread"]');

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  session().activeRoomId.set(1);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('the room list with email threads', () => {
  it('keeps a hidden thread out of the main list, in a collapsed group', async () => {
    session().rooms.set([
      room(1),
      thread(2, { name: 'Book club' }),
      thread(3, { name: 'Pappy’s', listed: true }),
    ]);
    renderPage();
    await waitFor(() => expect(mainNames()).toEqual(['Room 1', 'Pappy’s']));
    expect(groupToggle().textContent).toContain('1');
    // Collapsed: the group shows its count, not its rooms.
    expect(groupNames()).toEqual([]);
    await fireEvent.click(groupToggle());
    expect(groupNames()).toEqual(['Book club']);
  });

  it('draws no group when there is nothing hidden', async () => {
    session().rooms.set([room(1), thread(2, { listed: true })]);
    renderPage();
    await waitFor(() => expect(mainNames()).toHaveLength(2));
    expect(screen.queryByRole('button', { name: /Email threads/ })).toBeNull();
  });

  it('opens the group when its room is the one on screen', async () => {
    session().rooms.set([room(1), thread(2, { name: 'Book club' })]);
    session().activeRoomId.set(2);
    renderPage();
    await waitFor(() => expect(groupNames()).toEqual(['Book club']));
    expect(document.querySelector('.room-list-threads .room-row.active')).toBeTruthy();
  });

  it('leaves a hidden thread’s unread out of the Unread total', async () => {
    session().rooms.set([
      room(1, { unread_count: 0 }),
      room(2, { unread_count: 2 }),
      thread(3, { unread_count: 5 }),
      thread(4, { unread_count: 3, listed: true }),
    ]);
    renderPage();
    await waitFor(() => expect(unreadPill()?.getAttribute('title')).toBe('5 unread'));
    // No pill on the group either: the note in the private room is the signal.
    expect(groupToggle().querySelector('[title$="unread"]')).toBeNull();
  });
});
