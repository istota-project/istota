import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, screen } from '@testing-library/svelte';
import { get, type Writable } from 'svelte/store';
import { __history } from '$app/navigation';

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
    set: (target, key: string, value) => {
      target[key] = value;
      return true;
    },
  });
  return { getChatSession: () => session };
});

import { getChatSession } from '$lib/stores/chat';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { ChatRoom, ChatView, User } from '$lib/api';
import type { ChatMessage } from '$lib/stores/segments';

const person = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: true,
  features: { chat: true, admin: true },
} as User;
const rooms = [
  { id: 1, token: 'room-a', name: 'Room A' },
  { id: 2, token: 'room-b', name: 'Room B' },
].map((room) => ({
  ...room,
  origin: 'web',
  talk_token: null,
  model: null,
  effort: null,
  brain: null,
  unread: 0,
  last_activity: '2026-01-01T12:00:00Z',
})) as ChatRoom[];
const session = getChatSession() as unknown as {
  rooms: Writable<ChatRoom[]>;
  activeRoomId: Writable<number | null>;
  messages: Writable<ChatMessage[]>;
  view: Writable<'room' | ChatView>;
  init: ReturnType<typeof vi.fn>;
  selectRoom: ReturnType<typeof vi.fn>;
  selectRoomByToken: ReturnType<typeof vi.fn>;
  selectView: ReturnType<typeof vi.fn>;
  jumpToTask: ReturnType<typeof vi.fn>;
  jumpToMsgId: ReturnType<typeof vi.fn>;
};
const renderPage = () => render(Harness, { component: Page, user: person });
const currentUrl = () => __history.entries[__history.index].url;

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  __history.reset('/istota/chat/');
  session.rooms.set(rooms);
  session.activeRoomId.set(1);
  session.view.set('room');
  session.messages.set([]);
  session.init = vi.fn(async () => undefined);
  session.selectRoom = vi.fn(async (id: number) => {
    session.activeRoomId.set(id);
    session.view.set('room');
    session.messages.set([]);
  });
  session.selectRoomByToken = vi.fn(async (token: string) => {
    const room = get(session.rooms).find((r) => r.token === token);
    if (!room) return false;
    await session.selectRoom(room.id);
    return true;
  });
  session.selectView = vi.fn(async (view: ChatView) => {
    session.view.set(view);
    session.activeRoomId.set(null);
    session.messages.set([]);
  });
  session.jumpToTask = vi.fn(async (token: string) => {
    await session.selectRoomByToken(token);
    return true;
  });
  session.jumpToMsgId = vi.fn(async () => true);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('chat selection history', () => {
  it('replaces a bare URL with the seeded room without adding an entry', async () => {
    renderPage();
    await waitFor(() => expect(currentUrl()).toBe('/istota/chat/?room=room-a'));
    expect(__history.entries).toHaveLength(1);
  });

  it('pushes sidebar rooms once and restores the previous room on Back', async () => {
    renderPage();
    await waitFor(() => expect(currentUrl()).toContain('room=room-a'));
    await fireEvent.click(screen.getByRole('button', { name: 'Room B', exact: true }));
    expect(currentUrl()).toBe('/istota/chat/?room=room-b');
    expect(__history.entries).toHaveLength(2);
    await fireEvent.click(screen.getByRole('button', { name: 'Room B', exact: true }));
    expect(__history.entries).toHaveLength(2);
    session.selectRoomByToken.mockClear();
    __history.back();
    await waitFor(() => expect(session.selectRoomByToken).toHaveBeenCalledWith('room-a'));
    expect(get(session.activeRoomId)).toBe(1);
    expect(__history.entries).toHaveLength(2);
  });

  it('pushes a view and then a room with mutually exclusive params', async () => {
    renderPage();
    await waitFor(() => expect(currentUrl()).toContain('room=room-a'));
    await fireEvent.click(screen.getByRole('button', { name: 'Unread', exact: true }));
    expect(currentUrl()).toBe('/istota/chat/?view=unread');
    expect(get(session.view)).toBe('unread');
    await fireEvent.click(screen.getByRole('button', { name: 'Room B', exact: true }));
    expect(currentUrl()).toBe('/istota/chat/?room=room-b');
    __history.back();
    await waitFor(() => expect(get(session.view)).toBe('unread'));
  });

  it('waits for initialization before applying the initial task deep link', async () => {
    __history.reset('/istota/chat/?room=room-b&task=5');
    let finish!: () => void;
    session.init = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    renderPage();
    await waitFor(() => expect(session.init).toHaveBeenCalled());
    expect(currentUrl()).toBe('/istota/chat/?room=room-b&task=5');
    expect(session.jumpToTask).not.toHaveBeenCalled();
    finish();
    await waitFor(() => expect(session.jumpToTask).toHaveBeenCalledWith('room-b', 5));
    expect(currentUrl()).toBe('/istota/chat/?room=room-b&task=5');
    expect(__history.entries).toHaveLength(1);
  });

  it.each(['0', '-1', '1.5', '9007199254740992', 'nope'])(
    'drops invalid task id %s while restoring the room',
    async (task) => {
      __history.reset(`/istota/chat/?room=room-b&task=${task}&view=unread`);
      renderPage();
      await waitFor(() => expect(session.selectRoomByToken).toHaveBeenCalledWith('room-b'));
      expect(session.jumpToTask).not.toHaveBeenCalled();
      expect(get(session.view)).toBe('room');
    },
  );

  it('replaces a store fallback without creating navigation', async () => {
    renderPage();
    await waitFor(() => expect(currentUrl()).toContain('room=room-a'));
    session.rooms.set([rooms[1]]);
    session.activeRoomId.set(2);
    await waitFor(() => expect(currentUrl()).toBe('/istota/chat/?room=room-b'));
    expect(__history.entries).toHaveLength(1);
  });

  it('returns to room A after clicking a search result in room B', async () => {
    session.messages.set([
      {
        cid: 1,
        role: 'system',
        text: '',
        segments: [],
        streaming: false,
        searchResults: {
          kind: 'search_results',
          query: 'falcon',
          text: '',
          results: [
            {
              source_type: 'conversation',
              summary: 'the falcon timeline',
              date: '2026-01-01',
              room_token: 'room-b',
              room_name: 'Room B',
              task_id: 42,
              talk_message_id: null,
              talk_link: null,
            },
          ],
        },
      },
    ]);
    const { container } = renderPage();
    await waitFor(() => expect(currentUrl()).toContain('room=room-a'));
    await fireEvent.click(container.querySelector('.jump-btn')!);
    expect(session.jumpToTask).toHaveBeenCalledWith('room-b', 42);
    expect(get(session.activeRoomId)).toBe(2);
    expect(currentUrl()).toBe('/istota/chat/?room=room-b&task=42');
    __history.back();
    await waitFor(() => expect(get(session.activeRoomId)).toBe(1));
    expect(session.selectRoomByToken).toHaveBeenLastCalledWith('room-a');
    expect(currentUrl()).toBe('/istota/chat/?room=room-a');
  });
  it('keeps the current URL when a saved search result names a missing room', async () => {
    session.rooms.set([rooms[0]]);
    session.messages.set([
      {
        cid: 1,
        role: 'system',
        text: '',
        segments: [],
        streaming: false,
        searchResults: {
          kind: 'search_results',
          query: 'falcon',
          text: '',
          results: [
            {
              source_type: 'conversation',
              summary: 'the falcon timeline',
              date: '2026-01-01',
              room_token: 'room-b',
              room_name: 'Room B',
              task_id: 42,
              talk_message_id: null,
              talk_link: null,
            },
          ],
        },
      },
    ]);
    const { container } = renderPage();
    await waitFor(() => expect(currentUrl()).toContain('room=room-a'));
    await fireEvent.click(container.querySelector('.jump-btn')!);
    expect(session.jumpToTask).toHaveBeenCalledWith('room-b', 42);
    expect(get(session.activeRoomId)).toBe(1);
    expect(currentUrl()).toBe('/istota/chat/?room=room-a');
    expect(__history.entries).toHaveLength(1);
  });
});
