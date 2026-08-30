/**
 * The chat store with no connection (ISSUE-202): the read cache, and the
 * outbox that holds what is written while it is gone.
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
import type { ChatSession } from './chat';
import { MAX_QUEUED_PER_ROOM, SEND_QUEUE_STORAGE_KEY } from './sendQueue';

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
// A real backing map rather than a stub, because the outbox tests below turn
// on what a *previous session* left in storage and on what this one writes
// back. Hoisted so both survive `vi.resetModules()`, and round-tripped through
// JSON exactly as the real `localStorage` pair is.
const persisted = vi.hoisted(() => {
  const store = new Map<string, string>();
  return {
    store,
    loadSetting: vi.fn((key: string, fallback: unknown) =>
      store.has(key) ? JSON.parse(store.get(key) as string) : fallback,
    ),
    saveSetting: vi.fn((key: string, value: unknown) => {
      store.set(key, JSON.stringify(value));
    }),
  };
});
vi.mock('$lib/stores/persisted', () => persisted);

const notices = vi.hoisted(() => ({
  notify: vi.fn(),
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('$lib/stores/notices', () => notices);

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
    persisted.store.clear();
    Object.values(notices).forEach((v) => v.mockReset());
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

  it('restores the transcript and rethrows when the server answers with a failure', async () => {
    // The cached paint must not survive a real failure: left up, it is a
    // 50-row tail with paging disabled and nothing saying the load broke,
    // which reads as a complete conversation.
    db.readTranscript.mockResolvedValue({
      roomId: 2,
      roomToken: 't2',
      messages: [row(1, 'cached')],
      oldestCursor: null,
      savedAt: Date.now(),
    });
    const s = await freshSession();
    await s.init();
    const before = get(s.messages);

    api.getRoomMessages.mockRejectedValue(new Error('API error: 500'));
    await expect(s.selectRoom(2)).rejects.toThrow('API error: 500');
    expect(get(s.messages)).toEqual(before);
    expect(get(s.offlineTranscript)).toBe(false);
    s.teardown();
  });

  it('stores a running turn without the status that would keep it spinning', async () => {
    api.getRoomMessages.mockResolvedValue(
      history([row(1, 'ask'), { ...row(2, 'partial'), task_id: 5, status: 'running' } as Row]),
    );
    const s = await freshSession();
    await s.init();

    const stored = db.writeTranscript.mock.calls[0][1].messages;
    expect(stored[1].status).toBeUndefined();
    // Everything else about the row is kept — it is a real turn, only not a
    // live one any more.
    expect(stored[1].text).toBe('partial');
    expect(stored[0]).toEqual(row(1, 'ask'));
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
    persisted.store.clear();
    Object.values(notices).forEach((v) => v.mockReset());
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

  it('seeds the cursor before streaming when init could not reach the server', async () => {
    // `since_id: 0` is not a neutral cursor — the server answers it with every
    // message the user can see, so an unseeded stream replays the whole
    // history into the transcript and every background room's badge.
    vi.useFakeTimers();
    conn.setOnline(false);
    db.readRooms.mockResolvedValue([room(1), room(2)]);
    api.getChatRooms.mockRejectedValue(new Error('Failed to fetch'));
    api.getRoomMessages.mockRejectedValue(new Error('Failed to fetch'));
    api.getRoomEvents.mockRejectedValue(new Error('Failed to fetch'));

    const s = await freshSession();
    await s.init();
    await vi.advanceTimersByTimeAsync(1600);

    // Every call so far is the limit-1 gate, never a tail read from zero.
    for (const call of api.getRoomEvents.mock.calls) expect(call.slice(0, 2)).toEqual([0, 1]);

    // The connection comes back; the next tick buys a cursor rather than a
    // backlog, and only then does the tail start from it.
    conn.setOnline(true);
    api.getRoomEvents.mockResolvedValue({
      events: [],
      cursor: 900,
      gap: false,
      deletion_cursor: 7,
    });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    await vi.advanceTimersByTimeAsync(1600);
    expect(api.getRoomEvents).toHaveBeenCalledWith(0, 1);
    await vi.advanceTimersByTimeAsync(1600);
    expect(api.getRoomEvents).toHaveBeenCalledWith(900, 0, 0, 7);
    s.teardown();
  });

  it('does not advance the stream cursor over a recovery whose reload never landed', async () => {
    // `recoverStream` moves `roomCursor` to what it scanned, on the premise
    // that the reload it just did covers everything up to there. A bounded
    // reload that times out reports a gap, so `loadHistory` now paints the
    // cache and returns instead of throwing — and the rows between the two
    // cursors would be skipped, never fetched and never streamed.
    vi.useFakeTimers();
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 100, gap: false });
    const s = await freshSession();
    await s.init();
    await vi.advanceTimersByTimeAsync(1600);
    expect(api.getRoomEvents).toHaveBeenCalledWith(100, 0, 0, 0);

    // The recovery's seed (limit=1) answers with a cursor well ahead; the
    // ordinary tail read keeps reporting where the client actually is, so an
    // advance to 500 can only have come from the recovery.
    api.getRoomEvents.mockImplementation(async (_cursor: number, limit: number) =>
      limit === 1
        ? { events: [], cursor: 500, gap: false }
        : { events: [], cursor: 100, gap: false },
    );
    // The room reload stalls past its bound, which the connectivity store
    // reads as a gap rather than as an answer.
    api.getRoomMessages.mockRejectedValue(new Error('The operation was aborted'));
    conn.setOnline(false);

    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(90_000);
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(1600);

    // Still asking from where it actually got to.
    expect(api.getRoomEvents).toHaveBeenCalledWith(100, 0, 0, 0);
    expect(api.getRoomEvents).not.toHaveBeenCalledWith(500, 0, 0, 0);
    s.teardown();
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

/**
 * The outbox: what happens to a message written with no connection.
 *
 * `connectivity` is the hand-rolled store above, so these tests say what the
 * app believes rather than driving a probe schedule to make it believe it —
 * which is also why `noteTransport` is a spy here and a failed POST does not
 * flip the store by itself. The `localStorage` half is real (`persisted`
 * above), because "the entry is still there" is most of what an outbox
 * promises.
 */
describe('chat store — the text outbox', () => {
  beforeEach(() => {
    for (const bag of [api, db]) {
      Object.values(bag).forEach((v) => {
        if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
      });
    }
    conn.setOnline(true);
    persisted.store.clear();
    Object.values(notices).forEach((v) => v.mockReset());
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

  /** What is stored for a room right now. */
  function storedQueue(token: string): Record<string, unknown>[] {
    const raw = persisted.store.get(SEND_QUEUE_STORAGE_KEY);
    return raw ? (JSON.parse(raw)[`alice:room:${token}`] ?? []) : [];
  }

  /** Seed the stored map, as a previous session would have left it. */
  function seedQueue(token: string, entries: Record<string, unknown>[]) {
    persisted.store.set(
      SEND_QUEUE_STORAGE_KEY,
      JSON.stringify({ [`alice:room:${token}`]: entries }),
    );
  }

  function storedEntry(text: string, over: Record<string, unknown> = {}) {
    return {
      cid: 1,
      text,
      attachments: [],
      held: false,
      queuedAt: Date.now(),
      reason: 'busy',
      ...over,
    };
  }

  const queuedRow = (s: ChatSession, text: string) => get(s.messages).find((m) => m.text === text);

  const ok = { ok: true, status: 200, task_id: 43 };
  const gap = { ok: false, status: 0, failure: 'unreachable' };

  describe('entering the queue', () => {
    it('queues a send made offline without attempting a POST', async () => {
      const s = await freshSession();
      await s.init();
      conn.setOnline(false);

      await s.send('written in a lift');

      // No POST at all: the app already knows the answer, and asking again
      // costs a 30s timeout to be told it.
      expect(api.sendChatMessage).not.toHaveBeenCalled();
      const row = queuedRow(s, 'written in a lift');
      expect(row?.sendState).toBe('queued');
      expect(row?.queueReason).toBe('offline');
      expect(row?.queueHeld).toBeUndefined();
      expect(storedQueue('t1')).toMatchObject([
        { text: 'written in a lift', reason: 'offline', held: false },
      ]);
      s.teardown();
    });

    it('parks a send that discovered the gap at the head of the queue', async () => {
      // Restored held from a previous session, so it sits behind whatever is
      // written now: this message was typed first and has to go first.
      seedQueue('t1', [storedEntry('queued earlier')]);
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue(gap);

      await s.send('typed just now');

      const row = queuedRow(s, 'typed just now');
      expect(row?.sendState).toBe('queued');
      expect(row?.queueReason).toBe('offline');
      expect(storedQueue('t1').map((e) => e.text)).toEqual(['typed just now', 'queued earlier']);
      // The entry behind it is untouched: nothing failed, so the hold rule has
      // nothing to say here.
      expect(storedQueue('t1')[1].held).toBe(true);
      s.teardown();
    });

    it('holds a timed-out send and re-POSTs it with the key it was minted with', async () => {
      // A timeout is ambiguous — the task may exist — which is exactly what
      // the idempotency key is for: the replay resolves to the first task
      // rather than creating a second one.
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'timeout' });

      await s.send('did that arrive?');

      const key = api.sendChatMessage.mock.calls[0][5];
      expect(key).toEqual(expect.any(String));
      expect(queuedRow(s, 'did that arrive?')?.sendState).toBe('queued');
      expect(storedQueue('t1')[0].idempotencyKey).toBe(key);

      api.sendChatMessage.mockResolvedValue(ok);
      conn.setOnline(false);
      conn.setOnline(true);
      await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalledTimes(2));
      expect(api.sendChatMessage.mock.calls[1][5]).toBe(key);
      s.teardown();
    });

    it('signals the composer that the queue holds the durable copy now', async () => {
      // The composer keeps the submitted text as a draft until the ack, since
      // for an ordinary send that draft is the only copy that survives a
      // reload. Parked, it is not — and two copies of one message is how it
      // gets sent twice.
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue(gap);

      await s.send('into the gap');

      expect(get(s.sendSettled).n).toBe(1);
      expect(get(s.sendSettled).token).toBe('t1');
      s.teardown();
    });

    it('fails a send outright rather than parking past the per-room cap', async () => {
      // Ten entries restored and held: the room is idle and online, so nothing
      // stops the eleventh being sent. Parking it would put 11 in memory while
      // storage keeps the first 10 — a queued row on screen whose stored copy
      // was the one dropped, which is the hazard `enqueueSend` refuses at the
      // same cap to avoid.
      seedQueue(
        't1',
        Array.from({ length: MAX_QUEUED_PER_ROOM }, (_, i) =>
          storedEntry(`held ${i}`, { cid: i + 1 }),
        ),
      );
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue(gap);

      await s.send('the eleventh');

      const row = queuedRow(s, 'the eleventh');
      expect(row?.sendState).toBe('failed');
      expect(row?.retryable).toBe(true);
      expect(storedQueue('t1')).toHaveLength(MAX_QUEUED_PER_ROOM);
      expect(storedQueue('t1').some((e) => e.text === 'the eleventh')).toBe(false);
      s.teardown();
    });

    it('names the failure honestly on the path where parking is refused', async () => {
      // The two sentences for the gap failures are still live code, and this
      // is the only path that reaches them. A timeout is the one that has to
      // stay distinguishable: "no answer" is not "no server".
      const s = await freshSession();
      await s.init();
      let release: (v: unknown) => void = () => {};
      api.sendChatMessage.mockReturnValue(
        new Promise((r) => {
          release = r;
        }),
      );
      const sending = s.send('into the void');
      s.rooms.set([]);
      release({ ok: false, status: 0, failure: 'timeout' });
      await sending;

      expect(queuedRow(s, 'into the void')?.sendError).toMatch(/didn’t respond/);
      s.teardown();
    });

    it('fails a send outright when there is nowhere to park it', async () => {
      // The room left `$rooms` while the POST was open — another device's
      // delete, or a room list that came back short. There is no queue to hold
      // the message in, so the failed row and its Retry are what is left.
      const s = await freshSession();
      await s.init();
      let release: (v: unknown) => void = () => {};
      api.sendChatMessage.mockReturnValue(
        new Promise((r) => {
          release = r;
        }),
      );
      const sending = s.send('into the void');
      s.rooms.set([]);
      release(gap);
      await sending;

      const row = queuedRow(s, 'into the void');
      expect(row?.sendState).toBe('failed');
      expect(row?.sendError).toMatch(/unreachable/i);
      expect(row?.retryable).toBe(true);
      s.teardown();
    });
  });

  describe('leaving the queue', () => {
    async function twoQueuedOffline() {
      const s = await freshSession();
      await s.init();
      conn.setOnline(false);
      await s.send('first');
      await s.send('second');
      expect(api.sendChatMessage).not.toHaveBeenCalled();
      return s;
    }

    it('sends nothing while offline, however ready the room is', async () => {
      const s = await twoQueuedOffline();
      api.sendChatMessage.mockResolvedValue(ok);
      const cid = queuedRow(s, 'first')!.cid;

      // Release is the most direct route to a drain: an idle room, an unheld
      // head, nothing streaming. Only `online` is missing.
      await s.releaseQueued(cid);

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(storedQueue('t1')).toHaveLength(2);
      s.teardown();
    });

    it('reconciles the transcript before it drains, and drains one entry', async () => {
      const s = await twoQueuedOffline();
      const order: string[] = [];
      api.getRoomMessages.mockImplementation(async () => {
        order.push('history');
        return emptyHistory;
      });
      api.sendChatMessage.mockImplementation(async () => {
        order.push('send');
        return ok;
      });

      conn.setOnline(true);

      await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalledTimes(1));
      // A queued message going out into a transcript this client has not
      // caught up on would answer something that was already answered.
      expect(order[0]).toBe('history');
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('first');
      // One entry per drain: the next goes when this turn settles.
      expect(storedQueue('t1').map((e) => e.text)).toEqual(['second']);
      s.teardown();
    });

    it('leaves a drained entry where it is when the gap is still there', async () => {
      const s = await twoQueuedOffline();
      // The real `sendChatMessage` reports every completion to the
      // connectivity store on its way out, so by the time the store sees this
      // failure it is offline again — which is what stops the reconnect
      // draining a second time into the same gap. Mocked out, that has to be
      // said here or the test would be asserting against a store no
      // production run ever holds.
      api.sendChatMessage.mockImplementation(async () => {
        conn.setOnline(false);
        return gap;
      });

      conn.setOnline(true);
      await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalledTimes(1));

      const row = queuedRow(s, 'first');
      expect(row?.sendState).toBe('queued');
      expect(row?.queueReason).toBe('offline');
      expect(row?.queueHeld).toBeUndefined();
      // Order preserved by doing nothing, which is the point of leaving the
      // entry in place rather than unshifting it back.
      expect(storedQueue('t1').map((e) => e.text)).toEqual(['first', 'second']);
      // And nothing is held: a gap is not a turn that ended badly.
      expect(storedQueue('t1').every((e) => e.held === false)).toBe(true);
      s.teardown();
    });

    it('folds the echo of a parked send into its row instead of sending it again', async () => {
      // The ambiguous timeout, resolved: the server did have the message, and
      // its canonical row arrives over the room stream. Without the adoption
      // the transcript would show the message twice and the queue would POST
      // it again.
      vi.useFakeTimers();
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'timeout' });
      await s.send('did that arrive?');
      expect(storedQueue('t1')).toHaveLength(1);

      api.getRoomEvents.mockResolvedValueOnce({
        events: [row(50, 'did that arrive?', { role: 'user', task_id: 7 })],
        cursor: 50,
        gap: false,
      });
      await vi.advanceTimersByTimeAsync(2000);

      const mine = get(s.messages).filter((m) => m.role === 'user');
      expect(mine).toHaveLength(1);
      expect(mine[0].sendState).toBeUndefined();
      expect(mine[0].taskId).toBe(7);
      expect(storedQueue('t1')).toEqual([]);
      s.teardown();
    });

    it('drops a parked row the reconnect finds in the server’s own history', async () => {
      // The same case, arriving the other way. `onBackOnline` reconciles first,
      // and a rebuild carries client-only rows back on top of whatever it
      // painted — so the queued mirror of a message the server already has
      // would sit under the server's copy of it, and then be POSTed again.
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'timeout' });
      await s.send('did that arrive?');

      api.getRoomMessages.mockResolvedValue(
        history([row(50, 'did that arrive?', { role: 'user', task_id: 7 })]),
      );
      conn.setOnline(false);
      conn.setOnline(true);

      // Waiting on the *server's* row, not on a count: the parked row alone
      // already satisfies a count of one, so a count would pass before the
      // rebuild it is meant to observe.
      await vi.waitFor(() => expect(get(s.messages).some((m) => m.msgId === 50)).toBe(true));
      expect(get(s.messages).filter((m) => m.role === 'user')).toHaveLength(1);
      expect(storedQueue('t1')).toEqual([]);
      // One POST in the whole test: the parked one. Nothing re-sent it.
      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      s.teardown();
    });

    it('counts what is waiting in each room for the room list', async () => {
      const s = await twoQueuedOffline();
      await s.selectRoom(2);
      await s.send('for the other room');

      expect(get(s.queuedCounts)).toEqual({ t1: 2, t2: 1 });

      // And it follows the queue back down.
      s.removeQueued(queuedRow(s, 'for the other room')!.cid);
      expect(get(s.queuedCounts)).toEqual({ t1: 2 });
      s.teardown();
    });
  });

  describe('restoring across a relaunch', () => {
    const HOUR = 60 * 60 * 1000;

    it('sends a recent offline entry on its own', async () => {
      // The whole point of storing the reason: this message was written to a
      // server that could not be reached, and a relaunch is exactly when it
      // should go.
      seedQueue('t1', [
        storedEntry('written in a lift', { reason: 'offline', queuedAt: Date.now() - HOUR }),
      ]);
      api.sendChatMessage.mockResolvedValue(ok);
      const s = await freshSession();

      await s.init();
      await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalledTimes(1));

      expect(api.sendChatMessage.mock.calls[0][1]).toBe('written in a lift');
      s.teardown();
    });

    it('holds an offline entry older than a day', async () => {
      // Firing into a conversation that has moved on, while the user is
      // looking at something else, is the surprise the hold exists to prevent.
      seedQueue('t1', [
        storedEntry('written on Tuesday', { reason: 'offline', queuedAt: Date.now() - 25 * HOUR }),
      ]);
      api.sendChatMessage.mockResolvedValue(ok);
      const s = await freshSession();

      await s.init();

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRow(s, 'written on Tuesday')?.queueHeld).toBe(true);
      s.teardown();
    });

    it('keeps a hold the last session applied to an offline entry', async () => {
      // `holdRoomQueue` marks every entry in a room, offline ones included, so
      // a Stop on the turn ahead of them is stored as `held`. The age rule may
      // keep a hold; it must never clear one, or three messages queued in a
      // lift and then deliberately held would fire on the next page load.
      seedQueue('t1', [
        storedEntry('held by a Stop', {
          reason: 'offline',
          queuedAt: Date.now() - HOUR,
          held: true,
        }),
      ]);
      api.sendChatMessage.mockResolvedValue(ok);
      const s = await freshSession();

      await s.init();

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRow(s, 'held by a Stop')?.queueHeld).toBe(true);
      s.teardown();
    });

    it('holds a command queued offline, however recently', async () => {
      // A command is answered inside the request, so re-sending one later is
      // not a repeat of the same message — `!steer` is a second note against a
      // turn that is over by then. `send()` files it as a busy entry for that
      // reason, and this is what that buys.
      const s = await freshSession();
      await s.init();
      conn.setOnline(false);
      await s.send('!steer check the other repo too');
      expect(storedQueue('t1')[0].reason).toBe('busy');
      s.teardown();

      const relaunched = await freshSession();
      api.sendChatMessage.mockResolvedValue(ok);
      await relaunched.init();

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRow(relaunched, '!steer check the other repo too')?.queueHeld).toBe(true);
      relaunched.teardown();
    });

    it('holds a busy entry however recent it is', async () => {
      // The turn it was written against is over and unobserved. Unchanged by
      // any of this (ISSUE-238).
      seedQueue('t1', [storedEntry('typed behind a turn', { queuedAt: Date.now() - 60_000 })]);
      api.sendChatMessage.mockResolvedValue(ok);
      const s = await freshSession();

      await s.init();

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRow(s, 'typed behind a turn')?.queueHeld).toBe(true);
      s.teardown();
    });
  });
});
