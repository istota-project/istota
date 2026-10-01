/**
 * The sidebar marks a room more than one human reads.
 *
 * Multiplayer made a room's audience the thing a sender most needs to know
 * before typing, and the room list is where the room is chosen. A shared room
 * takes a people glyph in the leading slot instead of its origin glyph; a
 * shared Talk room keeps the Talk fact in the glyph's title, so nothing the
 * origin glyph said is lost.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';

// `vi.hoisted`, because `vi.mock`'s factory is lifted above every top-level
// declaration in the file and would otherwise read this before it exists.
const { brainCatalogue } = vi.hoisted(() => ({
  brainCatalogue: vi.fn(async () => [
    { kind: 'native', label: 'Native', model_namespace: 'native' },
    { kind: 'claude_code', label: 'Claude Code', model_namespace: 'claude' },
  ]),
}));

vi.mock('$lib/components/chat/autocomplete/providers', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, getSelectableBrains: () => brainCatalogue() };
});

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
  is_admin: true,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: false,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: true,
  },
};

const room = (over: Record<string, unknown> = {}) => ({
  id: 1,
  token: 'web-1',
  name: 'Testing',
  origin: 'web',
  talk_token: null,
  model: null,
  effort: null,
  brain: null,
  unread: 0,
  last_activity: new Date().toISOString(),
  ...over,
});

const glyph = (name: string) =>
  [...document.querySelectorAll<HTMLElement>('.room-row')]
    .find((row) => row.querySelector('.room-name')?.textContent?.trim() === name)
    ?.querySelector<HTMLElement>('.room-origin');

const setRooms = (list: Record<string, unknown>[]) => {
  const session = getChatSession() as unknown as {
    rooms: { set: (v: unknown) => void };
  };
  session.rooms.set(list.map((over, i) => room({ id: i + 1, token: `web-${i + 1}`, ...over })));
};

const renderPage = () => render(Harness, { component: Page, user: person });

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('the shared room glyph', () => {
  it('marks a shared room and leaves a private one with its origin glyph', async () => {
    setRooms([
      { name: 'Solo', shared: false },
      { name: 'Team', shared: true },
    ]);
    renderPage();
    await waitFor(() => expect(glyph('Team')).toBeTruthy());
    expect(glyph('Team')!.classList.contains('shared')).toBe(true);
    expect(glyph('Team')!.getAttribute('aria-label')).toBe('Shared room');
    expect(glyph('Solo')!.classList.contains('shared')).toBe(false);
    expect(glyph('Solo')!.getAttribute('title')).toBe('Web room');
  });

  it('keeps the Talk fact in the title of a shared Talk room', async () => {
    setRooms([
      { name: 'Group', shared: true, origin: 'talk' },
      { name: 'Pair', shared: false, origin: 'talk' },
    ]);
    renderPage();
    await waitFor(() => expect(glyph('Group')).toBeTruthy());
    expect(glyph('Group')!.classList.contains('shared')).toBe(true);
    expect(glyph('Group')!.getAttribute('title')).toBe('Shared room, also on Nextcloud Talk');
    expect(glyph('Pair')!.classList.contains('talk')).toBe(true);
  });

  it('treats a room from an older backend, with no shared key, as private', async () => {
    setRooms([{ name: 'Old' }]);
    renderPage();
    await waitFor(() => expect(glyph('Old')).toBeTruthy());
    expect(glyph('Old')!.classList.contains('shared')).toBe(false);
  });
});
