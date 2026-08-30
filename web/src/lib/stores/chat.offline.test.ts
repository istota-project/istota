/**
 * The chat store against the offline read cache (ISSUE-202, stage 2).
 *
 * Covers what `offline/db.test.ts` cannot: when the cache is read, when it is
 * written, and what the transcript does when a fetch comes back with nothing
 * because there is no connection. The storage layer is mocked here rather than
 * driven through `fake-indexeddb` — these are assertions about the store's
 * decisions, and a real database would make them assertions about both.
 *
 * `$lib/stores/connectivity` is mocked too, with a hand-rolled store, because
 * every question here turns on what the app believes about reachability and
 * driving the real one would mean driving its probe schedule as well.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatHistory, ChatRoom } from '$lib/api';

const api = vi.hoisted(() => ({
  getChatConfig: vi.fn(),
  getChatRooms: vi.fn(),
  getRoomMessages: vi.fn(),
  getChatMessagesView: vi.fn(),
  getRoomEvents: vi.fn(),
  chatRoomStreamUrl: vi.fn(() => '/stream'),
  chatStreamUrl: vi.fn(() => '/task-stream'),
  markRoomRead: vi.fn(),
  markAllRoomsRead: vi.fn(),
  setChatMessageStarred: vi.fn(),
  deleteChatMessage: vi.fn(),
  getTaskEvents: vi.fn(),
  sendChatMessage: vi.fn(),
  createChatRoom: vi.fn(),
  updateChatRoom: vi.fn(),
  deleteChatRoom: vi.fn(),
  promoteChatRoom: vi.fn(),
  cancelChatTask: vi.fn(),
  confirmChatTask: vi.fn(),
  getNotificationCounts: vi.fn(),
  ChatRoomBusyError: class extends Error {},
  ChatMessageBusyError: class extends Error {},
}));

const db = vi.hoisted(() => ({
  readTranscript: vi.fn(),
  writeTranscript: vi.fn(),
  appendTranscriptRows: vi.fn(),
  deleteTranscript: vi.fn(),
  removeCachedMessages: vi.fn(),
  readRooms: vi.fn(),
  writeRooms: vi.fn(),
  readConfig: vi.fn(),
  writeConfig: vi.fn(),
  pruneOffline: vi.fn(),
}));

const conn = vi.hoisted(() => {
  let value = true;
  const subscribers = new Set<(v: boolean) => void>();
  return {
    online: {
      subscribe(fn: (v: boolean) => void) {
        subscribers.add(fn);
        fn(value);
        return () => void subscribers.delete(fn);
      },
    },
    setOnline(next: boolean) {
      value = next;
      for (const fn of subscribers) fn(value);
    },
    noteTransport: vi.fn(),
    probe: vi.fn(),
    startConnectivity: vi.fn(() => () => {}),
  };
});

vi.mock('$lib/api', () => api);
vi.mock('$lib/offline/db', () => db);
vi.mock('$lib/stores/connectivity', () => conn);
vi.mock('$lib/stores/persisted', () => ({
  loadSetting: vi.fn(() => null),
  saveSetting: vi.fn(),
}));

function room(id: number, name = `Room ${id}`): ChatRoom {
  return {
    id,
    token: `t${id}`,
    name,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: 0,
  };
}

type Row = ChatHistory['messages'][number];

function row(msgId: number, text: string, over: Partial<Row> = {}): Row {
  return {
    role: 'assistant',
    text,
    created_at: '2026-08-30T10:00:00Z',
    msg_id: msgId,
    starred: false,
    room_token: 't1',
    ...over,
  } as Row;
}

const emptyHistory: ChatHistory = { messages: [], active_task: null, active_tasks: [] };

function history(messages: Row[], over: Partial<ChatHistory> = {}): ChatHistory {
  return { ...emptyHistory, messages, ...over };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — the offline read cache', () => {
  beforeEach(() => {
    for (const bag of [api, db]) {
      Object.values(bag).forEach((v) => {
        if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
      });
    }
    conn.setOnline(true);
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500, user_id: 'alice' });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatRoomStreamUrl.mockReturnValue('/stream');
    api.getTaskEvents.mockResolvedValue({ events: [] });
    db.readTranscript.mockResolvedValue(null);
    db.readRooms.mockResolvedValue(null);
    db.readConfig.mockResolvedValue(null);
    // No EventSource in jsdom, so the room stream falls through to polling —
    // the same funnel an SSE frame takes, one tick at a time.
    (globalThis as any).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('paints the cached transcript before the fetch resolves', async () => {
    const pending = deferred<ChatHistory>();
    api.getRoomMessages.mockReturnValue(pending.promise);
    db.readTranscript.mockResolvedValue({
      roomId: 1,
      roomToken: 't1',
      messages: [row(1, 'cached one'), row(2, 'cached two')],
      oldestCursor: { ts: '2026-08-29 09:00:00', id: 1 },
      savedAt: Date.now(),
    });

    const s = await freshSession();
    const running = s.init();
    await vi.waitFor(() => expect(get(s.messages)).toHaveLength(2));
    expect(get(s.messages).map((m) => m.text)).toEqual(['cached one', 'cached two']);
    expect(db.readTranscript).toHaveBeenCalledWith('alice', 't1');

    // …and the fetch, when it lands, replaces it wholesale.
    pending.resolve(history([row(9, 'from the server')], { has_more: true }));
    await running;
    expect(get(s.messages).map((m) => m.text)).toEqual(['from the server']);
    expect(get(s.hasMore)).toBe(true);
    expect(get(s.offlineTranscript)).toBe(false);
    s.teardown();
  });

  it('keeps the cached transcript when the fetch fails offline', async () => {
    conn.setOnline(false);
    api.getRoomMessages.mockRejectedValue(new Error('Failed to fetch'));
    db.readTranscript.mockResolvedValue({
      roomId: 1,
      roomToken: 't1',
      messages: [row(1, 'cached one')],
      oldestCursor: { ts: '2026-08-29 09:00:00', id: 1 },
      savedAt: Date.now(),
    });

    const s = await freshSession();
    await s.init();

    expect(get(s.messages).map((m) => m.text)).toEqual(['cached one']);
    expect(get(s.offlineTranscript)).toBe(true);
    // A cached tail has an older page behind it that offline cannot be
    // fetched, so the affordance is withheld rather than left to spin.
    expect(get(s.hasMore)).toBe(false);
    // Nothing was written over the cache from a load that returned nothing.
    expect(db.writeTranscript).not.toHaveBeenCalled();
    expect(db.deleteTranscript).not.toHaveBeenCalled();
    s.teardown();
  });

  it('still fails a room load that fails while the server is reachable', async () => {
    // The regression guard on the tolerance above: a 500 is not an outage, and
    // swallowing it would leave a stale transcript on screen with no report.
    api.getRoomMessages.mockRejectedValue(new Error('API error: 500'));
    const s = await freshSession();
    await s.init();
    await expect(s.selectRoom(2)).rejects.toThrow('API error: 500');
    s.teardown();
  });

  it('shows a room with no cache as empty rather than as a failure', async () => {
    conn.setOnline(false);
    api.getRoomMessages.mockRejectedValue(new Error('Failed to fetch'));
    const s = await freshSession();
    await s.init();

    expect(get(s.messages)).toEqual([]);
    expect(get(s.loaded)).toBe(true);
    expect(get(s.offlineTranscript)).toBe(true);
    s.teardown();
  });

  it('writes the fetched transcript through, and drops it when the room is empty', async () => {
    api.getRoomMessages.mockResolvedValue(
      history([row(1, 'one'), row(2, 'two')], {
        oldest_cursor: { ts: '2026-08-30 09:00:00', id: 1 },
      }),
    );
    const s = await freshSession();
    await s.init();

    expect(db.writeTranscript).toHaveBeenCalledWith('alice', {
      roomId: 1,
      roomToken: 't1',
      messages: [row(1, 'one'), row(2, 'two')],
      oldestCursor: { ts: '2026-08-30 09:00:00', id: 1 },
    });

    api.getRoomMessages.mockResolvedValue(emptyHistory);
    await s.selectRoom(2);
    expect(db.deleteTranscript).toHaveBeenCalledWith('alice', 't2');
    s.teardown();
  });

  it('paints the cached room list and reconciles it', async () => {
    const cached = [room(7, 'From the cache')];
    db.readRooms.mockResolvedValue(cached);
    const pending = deferred<{ rooms: ChatRoom[] }>();
    api.getChatRooms.mockReturnValue(pending.promise);

    const s = await freshSession();
    const running = s.init();
    await vi.waitFor(() => expect(get(s.rooms).map((r) => r.name)).toEqual(['From the cache']));

    pending.resolve({ rooms: [room(1), room(2)] });
    await running;
    expect(get(s.rooms).map((r) => r.id)).toEqual([1, 2]);
    expect(db.writeRooms).toHaveBeenCalledWith('alice', [room(1), room(2)]);
    s.teardown();
  });

  it('runs on the cached room list when the room fetch fails offline', async () => {
    conn.setOnline(false);
    db.readRooms.mockResolvedValue([room(1), room(2)]);
    api.getChatRooms.mockRejectedValue(new Error('Failed to fetch'));
    api.getRoomMessages.mockRejectedValue(new Error('Failed to fetch'));

    const s = await freshSession();
    await s.init();

    expect(get(s.rooms).map((r) => r.id)).toEqual([1, 2]);
    expect(get(s.activeRoomId)).toBe(1);
    expect(get(s.loaded)).toBe(true);
    // The cached list is not re-stored — that would push its own expiry out
    // every time the app opened without a connection.
    expect(db.writeRooms).not.toHaveBeenCalled();
    s.teardown();
  });

  it('gives up when the room fetch fails offline with nothing cached', async () => {
    conn.setOnline(false);
    api.getChatRooms.mockRejectedValue(new Error('Failed to fetch'));
    const s = await freshSession();
    await s.init();
    expect(get(s.loaded)).toBe(false);
    s.teardown();
  });

  it('falls back to the cached config when the live one does not answer', async () => {
    api.getChatConfig.mockRejectedValue(new Error('Failed to fetch'));
    db.readConfig.mockResolvedValue({
      client_poll_interval_ms: 4000,
      user_id: 'alice',
      external_turn_display: 'full',
    });

    const s = await freshSession();
    await s.init();

    expect(get(s.externalTurnDisplay)).toBe('full');
    // Read back, not written back.
    expect(db.writeConfig).not.toHaveBeenCalled();
    s.teardown();
  });

  it('stores the live config under the id it publishes', async () => {
    const s = await freshSession();
    await s.init();
    expect(db.writeConfig).toHaveBeenCalledWith('alice', {
      client_poll_interval_ms: 1500,
      user_id: 'alice',
    });
    s.teardown();
  });

  it('prunes expired entries on init', async () => {
    const s = await freshSession();
    await s.init();
    expect(db.pruneOffline).toHaveBeenCalled();
    s.teardown();
  });
});

describe('chat store — cache write-through from the room stream', () => {
  beforeEach(() => {
    for (const bag of [api, db]) {
      Object.values(bag).forEach((v) => {
        if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
      });
    }
    conn.setOnline(true);
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500, user_id: 'alice' });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [] });
    db.readTranscript.mockResolvedValue(null);
    db.readRooms.mockResolvedValue(null);
    db.readConfig.mockResolvedValue(null);
    (globalThis as any).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('folds a burst of frames into one write per room', async () => {
    vi.useFakeTimers();
    const background = row(11, 'in the other room', { room_token: 't2' });
    const foreground = row(12, 'in this room', { room_token: 't1' });
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 0, gap: false });
    api.getRoomEvents.mockResolvedValueOnce({
      events: [background, foreground, row(13, 'again', { room_token: 't2' })],
      cursor: 13,
      gap: false,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 13, gap: false });

    const s = await freshSession();
    await s.init();
    await vi.advanceTimersByTimeAsync(1600);
    // The debounce is still holding them.
    expect(db.appendTranscriptRows).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2000);

    expect(db.appendTranscriptRows).toHaveBeenCalledTimes(2);
    // A background room is cached exactly as the open one is — that is what
    // leaves it current when the user switches to it with no connection.
    expect(db.appendTranscriptRows).toHaveBeenCalledWith('alice', 't2', [
      background,
      row(13, 'again', { room_token: 't2' }),
    ]);
    expect(db.appendTranscriptRows).toHaveBeenCalledWith('alice', 't1', [foreground]);
    s.teardown();
  });

  it('takes a deleted message out of the cache and out of what is waiting to be written', async () => {
    vi.useFakeTimers();
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 0, gap: false });
    api.getRoomEvents.mockResolvedValueOnce({
      events: [row(20, 'doomed'), row(21, 'kept')],
      cursor: 21,
      gap: false,
    });
    api.getRoomEvents.mockResolvedValueOnce({
      events: [],
      cursor: 21,
      gap: false,
      deletions: [{ msg_id: 20 }],
      deletion_cursor: 5,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 21, gap: false });

    const s = await freshSession();
    await s.init();
    await vi.advanceTimersByTimeAsync(1600); // the rows land
    await vi.advanceTimersByTimeAsync(1600); // the deletion lands, still inside the debounce

    expect(db.removeCachedMessages).toHaveBeenCalledWith('alice', [20]);
    await vi.advanceTimersByTimeAsync(2000);
    expect(db.appendTranscriptRows).toHaveBeenCalledWith('alice', 't1', [row(21, 'kept')]);
    s.teardown();
  });

  it('flushes what the debounce is holding on teardown', async () => {
    vi.useFakeTimers();
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 0, gap: false });
    api.getRoomEvents.mockResolvedValueOnce({
      events: [row(30, 'last thing said')],
      cursor: 30,
      gap: false,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 30, gap: false });

    const s = await freshSession();
    await s.init();
    await vi.advanceTimersByTimeAsync(1600);
    expect(db.appendTranscriptRows).not.toHaveBeenCalled();

    s.teardown();
    expect(db.appendTranscriptRows).toHaveBeenCalledWith('alice', 't1', [
      row(30, 'last thing said'),
    ]);
  });

  it('drops a room cache when the room goes away', async () => {
    api.updateChatRoom.mockResolvedValue(room(2));
    const s = await freshSession();
    await s.init();
    await s.archiveRoom(2);
    expect(db.deleteTranscript).toHaveBeenCalledWith('alice', 't2');
    s.teardown();
  });
});
