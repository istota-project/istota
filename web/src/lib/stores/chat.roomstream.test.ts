/**
 * Live room-event stream — store behaviour (live-web-chat-room-stream spec,
 * stages 3–5).
 *
 * The session opens one user-scoped SSE connection carrying every room the
 * user is a member of. These tests drive it through the polling fallback (no
 * EventSource in jsdom, which is the same degradation path a buffering proxy
 * produces in production) and assert routing, dedup, the fast-turn fix, the
 * gap/recovery threshold, background badges + previews, and `room` frames.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory } from '$lib/api';

const api = vi.hoisted(() => ({
  getChatConfig: vi.fn(),
  getChatRooms: vi.fn(),
  getRoomMessages: vi.fn(),
  getChatMessagesView: vi.fn(),
  getRoomEvents: vi.fn(),
  chatRoomStreamUrl: vi.fn(() => '/stream'),
  setChatMessageStarred: vi.fn(),
  markAllRoomsRead: vi.fn(),
  markRoomRead: vi.fn(),
  getTaskEvents: vi.fn(),
  sendChatMessage: vi.fn(),
  createChatRoom: vi.fn(),
  updateChatRoom: vi.fn(),
  deleteChatRoom: vi.fn(),
  promoteChatRoom: vi.fn(),
  cancelChatTask: vi.fn(),
  confirmChatTask: vi.fn(),
  chatStreamUrl: vi.fn(() => '/task-stream'),
  ChatRoomBusyError: class extends Error {},
}));

vi.mock('$lib/api', () => api);
vi.mock('$lib/stores/persisted', () => ({
  loadSetting: vi.fn(() => null),
  saveSetting: vi.fn(),
}));

function room(id: number, unread = 0, name = `Room ${id}`): ChatRoom {
  return {
    id,
    token: `t${id}`,
    name,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: unread,
  };
}

type Row = ChatHistory['messages'][number] & { room_token: string };

function row(msgId: number, token: string, over: Partial<Row> = {}): Row {
  return {
    role: 'assistant',
    text: `msg ${msgId}`,
    created_at: '2026-07-26T10:00:00Z',
    msg_id: msgId,
    starred: false,
    room_token: token,
    room_name: 'Room',
    ...over,
  } as Row;
}

/** Queue one poll response; everything after it reports "nothing new". */
function queueEvents(events: Row[], cursor: number) {
  api.getRoomEvents.mockResolvedValueOnce({ events, cursor, gap: false });
  api.getRoomEvents.mockResolvedValue({ events: [], cursor, gap: false });
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

/** A minimal EventSource stand-in so the SSE branch (named `message` / `gap` /
 * `room` listeners) can be exercised — jsdom has none, and without one every
 * test would only ever cover the polling fallback. */
function installFakeEventSource(): { current: FakeEventSource | null } {
  const ref: { current: FakeEventSource | null } = { current: null };
  class FakeEventSource {
    listeners = new Map<string, ((e: any) => void)[]>();
    onerror: (() => void) | null = null;
    onopen: (() => void) | null = null;
    closed = false;
    constructor() {
      ref.current = this as unknown as FakeEventSource;
    }
    addEventListener(kind: string, fn: (e: any) => void) {
      const cur = this.listeners.get(kind) ?? [];
      cur.push(fn);
      this.listeners.set(kind, cur);
    }
    close() {
      this.closed = true;
    }
    emit(kind: string, payload: unknown, lastEventId = '') {
      for (const fn of this.listeners.get(kind) ?? []) {
        fn({ data: JSON.stringify(payload), lastEventId });
      }
    }
    fail() {
      this.onerror?.();
    }
  }
  (globalThis as any).EventSource = FakeEventSource;
  return ref as { current: FakeEventSource | null };
}
type FakeEventSource = {
  emit: (kind: string, payload: unknown, lastEventId?: string) => void;
  fail: () => void;
  onerror: (() => void) | null;
  onopen: (() => void) | null;
  closed: boolean;
};

const emptyHistory = { messages: [], active_task: null, active_tasks: [] };

describe('chat store — live room stream', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatRoomStreamUrl.mockReturnValue('/stream');
    api.chatStreamUrl.mockReturnValue('/task-stream');
    api.getTaskEvents.mockResolvedValue({ events: [] });
    // No EventSource in jsdom → startRoomStream falls through to polling,
    // which is the branch these tests drive.
    (globalThis as any).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('seeds the cursor from the server before connecting', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 41, gap: false });
    const s = await freshSession();
    await s.init();
    // limit=1 → a cursor, not the backlog the session just rendered.
    expect(api.getRoomEvents).toHaveBeenCalledWith(0, 1);
    s.teardown();
  });

  it('appends a message for the active room', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents([row(10, 't1', { text: 'from talk' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages).map((m) => m.text)).toContain('from talk');
    s.teardown();
  });

  it('bumps a background room badge and preview instead of appending', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2, 1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init(); // room 1 active
    queueEvents([row(10, 't2', { text: 'background news' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages)).toHaveLength(0);
    const r2 = get(s.rooms).find((r) => r.id === 2)!;
    expect(r2.unread_count).toBe(2);
    expect(r2.preview).toBe('background news');
    s.teardown();
  });

  it('does not ring a room for the user’s own mirrored turn', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2, 0)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents([row(10, 't2', { role: 'user', text: 'typed in Talk' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    // Matches count_unread_messages, which excludes role='user'.
    expect(get(s.rooms).find((r) => r.id === 2)!.unread_count).toBe(0);
    s.teardown();
  });

  it('opens a task stream for an in-flight turn from another surface', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'talk prompt', task_id: 77, status: 'running' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const msgs = get(s.messages);
    expect(msgs.map((m) => m.text)).toContain('talk prompt');
    // A placeholder bound to the task, and its stream started.
    expect(msgs.some((m) => m.role === 'assistant' && m.taskId === 77)).toBe(true);
    expect(get(s.activeTaskId)).toBe(77);
    s.teardown();
  });

  it('picks up a pending_confirmation turn (the old poller skipped it)', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'do it', task_id: 88, status: 'pending_confirmation' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.activeTaskId)).toBe(88);
    s.teardown();
  });

  it('does not open a task stream for a settled turn', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'old', task_id: 5, status: 'completed' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.activeTaskId)).toBeNull();
    s.teardown();
  });

  it('dedups a row already on screen and stamps its star key', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({
      ...emptyHistory,
      messages: [
        {
          role: 'assistant',
          text: 'already here',
          task_id: 3,
          status: 'completed',
          created_at: '2026-07-26T09:00:00Z',
        },
      ],
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { text: 'already here', task_id: 3, status: 'completed', starred: true })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const msgs = get(s.messages);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].msgId).toBe(10);
    expect(msgs[0].starred).toBe(true);
    s.teardown();
  });

  it('reloads on a gap instead of replaying the backlog', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const historyCalls = api.getRoomMessages.mock.calls.length;
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 900, gap: true });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 900, gap: false });
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(0);
    // Reloaded the open room + the room list rather than patching.
    expect(api.getRoomMessages.mock.calls.length).toBeGreaterThan(historyCalls);
    s.teardown();
  });

  it('applies a room rename frame to the sidebar', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1, 0, 'old name')] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    es.current!.emit('room', {
      action: 'upsert',
      room: { id: 1, token: 't1', name: 'new name', origin: 'web', model: null, effort: null },
    });
    expect(get(s.rooms)[0].name).toBe('new name');
    s.teardown();
  });

  it('applies a room removal frame and moves off the deleted room', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init(); // room 1 active
    es.current!.emit('room', { action: 'remove', token: 't1', id: 1 });
    expect(get(s.rooms).map((r) => r.id)).toEqual([2]);
    s.teardown();
  });

  it('ignores a redelivered row via the durable-id guard', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    es.current!.emit('message', row(10, 't1', { text: 'once' }), '10');
    es.current!.emit('message', row(10, 't1', { text: 'once' }), '10');
    expect(get(s.messages).filter((m) => m.text === 'once')).toHaveLength(1);
    s.teardown();
  });

  it('falls back to polling when the stream errors', async () => {
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const before = api.getRoomEvents.mock.calls.length;
    es.current!.fail();
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.getRoomEvents.mock.calls.length).toBeGreaterThan(before);
    s.teardown();
  });

  it('feeds the All view live instead of leaving it a frozen snapshot', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getChatMessagesView.mockResolvedValue({
      messages: [],
      has_more: false,
      oldest_cursor: null,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    await s.selectView('all');
    queueEvents([row(10, 't2', { text: 'aggregate live' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages).map((m) => m.text)).toContain('aggregate live');
    s.teardown();
  });

  it('keeps the user’s own turns out of the Unread view', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getChatMessagesView.mockResolvedValue({
      messages: [],
      has_more: false,
      oldest_cursor: null,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    await s.selectView('unread');
    queueEvents([row(10, 't2', { role: 'user', text: 'mine' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages)).toHaveLength(0);
    s.teardown();
  });

  it('stops polling on teardown', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    s.teardown();
    const calls = api.getRoomEvents.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);
    expect(api.getRoomEvents.mock.calls.length).toBe(calls);
  });
});
