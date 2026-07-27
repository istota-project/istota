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
function installFakeEventSource(): { current: FakeEventSource | null; opened: number } {
  const ref: { current: FakeEventSource | null; opened: number } = {
    current: null,
    opened: 0,
  };
  class FakeEventSource {
    listeners = new Map<string, ((e: any) => void)[]>();
    onerror: (() => void) | null = null;
    onopen: (() => void) | null = null;
    closed = false;
    // 0 = CONNECTING (the browser is retrying on its own), 1 = OPEN,
    // 2 = CLOSED. Left undefined by default so the pre-existing tests keep
    // exercising the "fatal error → poll" branch.
    readyState: number | undefined = undefined;
    constructor() {
      ref.current = this as unknown as FakeEventSource;
      ref.opened += 1;
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
  return ref as { current: FakeEventSource | null; opened: number };
}
type FakeEventSource = {
  emit: (kind: string, payload: unknown, lastEventId?: string) => void;
  fail: () => void;
  onerror: (() => void) | null;
  onopen: (() => void) | null;
  closed: boolean;
  readyState: number | undefined;
};

const emptyHistory = { messages: [], active_task: null, active_tasks: [] };

/** Drive the visibilitychange listener through a hidden period of `ms`. */
async function hideFor(ms: number) {
  const set = (v: string) =>
    Object.defineProperty(document, 'visibilityState', { value: v, configurable: true });
  set('hidden');
  document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(ms);
  set('visible');
  document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(0);
}

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

  it('seeds the cursor before reading history, not after', async () => {
    // Capture-before-reload: a row committed between the two reads must be
    // re-delivered by the stream and dropped by the msg_id dedup. Seeding
    // afterwards puts it below the cursor AND outside the rendered page — and
    // the markRoomRead that follows consumes it, so it isn't even unread.
    const order: string[] = [];
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockImplementation(async () => {
      order.push('cursor');
      return { events: [], cursor: 41, gap: false };
    });
    api.getRoomMessages.mockImplementation(async () => {
      order.push('history');
      return emptyHistory;
    });
    const s = await freshSession();
    await s.init();
    expect(order.slice(0, 2)).toEqual(['cursor', 'history']);
    s.teardown();
  });

  it('abandons an init that was torn down mid-load', async () => {
    // onMount does not await init() and onDestroy tears down regardless, so
    // without a generation guard the rest of init runs on a page the user has
    // left — installing a stream, a 30s timer and a visibility listener, one
    // more of each per remount (only the newest listener is ever removed).
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    let release: (v: any) => void = () => {};
    api.getRoomMessages.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const s = await freshSession();
    const loading = s.init();
    await vi.advanceTimersByTimeAsync(0);
    s.teardown(); // navigate away mid-load
    release(emptyHistory);
    await loading;
    await vi.advanceTimersByTimeAsync(0);
    expect(es.opened).toBe(0);
    const roomsCalls = api.getChatRooms.mock.calls.length;
    await vi.advanceTimersByTimeAsync(35000); // the 30s reconciler never started
    expect(api.getChatRooms.mock.calls.length).toBe(roomsCalls);
    // ...and no listener survived to fire a mark-read from the abandoned page.
    api.markRoomRead.mockClear();
    await hideFor(1000);
    expect(api.markRoomRead).not.toHaveBeenCalled();
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
    // An assistant body belongs to the task stream — never overwritten here.
    expect(msgs[0].text).toBe('already here');
    s.teardown();
  });

  it('adopts the canonical body when deduping our own user turn', async () => {
    // The server does not always store what was typed: an attachment-only send
    // becomes a descriptor and a `!model …` prefix is stripped. Keeping the raw
    // text would leave web showing something Talk, a reload and the LLM's own
    // context all disagree with.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({
      ...emptyHistory,
      messages: [
        {
          role: 'user',
          text: '!model opus summarise this',
          task_id: 4,
          created_at: '2026-07-26T09:00:00Z',
        },
      ],
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'summarise this', task_id: 4, status: 'completed' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const msgs = get(s.messages);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].text).toBe('summarise this');
    expect(msgs[0].msgId).toBe(10);
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

  it('does not duplicate a turn when our own echo beats the send response', async () => {
    // The server writes the canonical user row inside the POST — and, with
    // user-scoped OAuth on, before a bounded ~5s Talk mirror — so the frame can
    // arrive while the bubble on screen still has no task_id to dedup against.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();

    let release: (v: any) => void = () => {};
    api.sendChatMessage.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const sending = s.send('hello');
    // The echo lands mid-POST: user row first, then nothing else.
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'hello', task_id: 7, status: 'running' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    release({ ok: true, task_id: 7 });
    await sending;
    await vi.advanceTimersByTimeAsync(0);

    const msgs = get(s.messages);
    expect(msgs.filter((m) => m.role === 'user')).toHaveLength(1);
    expect(msgs.filter((m) => m.role === 'assistant' && m.taskId === 7)).toHaveLength(1);
    // The durable id still reached the bubble, so it is starrable without a reload.
    expect(msgs.find((m) => m.role === 'user')!.msgId).toBe(10);
    s.teardown();
  });

  it('does not re-count a buffered row the recovery refresh already counted', async () => {
    // recoverStream buffers frames while it reloads, then drains them. Its own
    // refreshRooms returns server-computed counts that already include a row
    // written before that call, so bumping it again would inflate the badge.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2, 0)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init(); // room 1 active

    // The gap reload hands back room 2 with the server's count of 1 — the very
    // row that arrives as a frame while the reload is in flight.
    let releaseHistory: (v: any) => void = () => {};
    api.getRoomMessages.mockReturnValue(
      new Promise((res) => {
        releaseHistory = res;
      }),
    );
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2, 1)] });
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 900, gap: true });
    api.getRoomEvents.mockResolvedValueOnce({
      events: [row(901, 't2', { text: 'counted once' })],
      cursor: 901,
      gap: false,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 901, gap: false });
    await vi.advanceTimersByTimeAsync(2000); // gap → recovery starts, reload pends
    await vi.advanceTimersByTimeAsync(2000); // the frame lands and is buffered
    releaseHistory(emptyHistory);
    await vi.advanceTimersByTimeAsync(0);

    expect(get(s.rooms).find((r) => r.id === 2)!.unread_count).toBe(1);
    expect(get(s.rooms).find((r) => r.id === 2)!.preview).toBe('counted once');
    s.teardown();
  });

  it('does not wedge the live path when a recovery reload never settles', async () => {
    // recoverStream buffers every frame while it reloads and releases only in
    // its finally, so an unbounded fetch would swallow frames forever and the
    // `recovering` guard would refuse every future attempt.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    // A reload that hangs until its abort fires.
    api.getRoomMessages.mockImplementation(
      (_id: number, opts: { timeoutMs?: number } = {}) =>
        new Promise((_res, rej) => {
          setTimeout(() => rej(new Error('aborted')), opts.timeoutMs ?? 1e9);
        }),
    );
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 900, gap: true });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 900, gap: false });
    await vi.advanceTimersByTimeAsync(2000);
    // Past the recovery bound the state is released...
    await vi.advanceTimersByTimeAsync(20000);
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    queueEvents([row(950, 't1', { text: 'after the hang' })], 950);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages).map((m) => m.text)).toContain('after the hang');
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

  it('keeps SSE through a transient error the browser is already retrying', async () => {
    // Free reconnect is one of the reasons this is SSE and not a WebSocket;
    // closing on the first blip would throw it away and downgrade a
    // session-lived connection to polling for the rest of the day.
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const before = api.getRoomEvents.mock.calls.length;
    es.current!.readyState = 0; // CONNECTING — retry already scheduled
    es.current!.fail();
    await vi.advanceTimersByTimeAsync(5000);
    expect(es.current!.closed).toBe(false);
    expect(api.getRoomEvents.mock.calls.length).toBe(before);
    s.teardown();
  });

  it('concedes to polling after repeated failures, then re-probes SSE', async () => {
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const opened = es.opened;
    es.current!.readyState = 0;
    es.current!.fail();
    es.current!.fail();
    es.current!.fail(); // third consecutive → give up on the connection
    expect(es.current!.closed).toBe(true);
    const polled = api.getRoomEvents.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3000);
    expect(api.getRoomEvents.mock.calls.length).toBeGreaterThan(polled);
    // ...and the poll loop re-probes SSE rather than polling forever.
    await vi.advanceTimersByTimeAsync(61000);
    expect(es.opened).toBeGreaterThan(opened);
    s.teardown();
  });

  it('recovers on reconnect after a long silence, but not on the first open', async () => {
    // The client-side half of the gap threshold: past ROOM_STREAM_STALE_MS a
    // reconnect has probably missed state the stream does not carry, so a
    // reload is more correct than trusting the delta. The first open follows a
    // fresh history load, so it must not recover.
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    es.current!.onopen!();
    await vi.advanceTimersByTimeAsync(0);
    const afterFirstOpen = api.getRoomMessages.mock.calls.length;

    await vi.advanceTimersByTimeAsync(61000); // silence past the stale window
    es.current!.onopen!(); // reconnected
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getRoomMessages.mock.calls.length).toBeGreaterThan(afterFirstOpen);
    s.teardown();
  });

  it('reloads after a long hidden period when the connection did not hold', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init(); // no EventSource in jsdom → polling, so nothing "held"
    const before = api.getRoomMessages.mock.calls.length;
    await hideFor(61000);
    expect(api.getRoomMessages.mock.calls.length).toBeGreaterThan(before);
    s.teardown();
  });

  it('reconciles metadata only when the connection held across the hidden period', async () => {
    // A stream that stayed open cannot have missed a `messages` row, so tearing
    // down the transcript (and with it a healthy in-flight task stream, which
    // would re-render its answer from seq 0) buys nothing.
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    es.current!.onopen!(); // connection is live
    await vi.advanceTimersByTimeAsync(0);
    const history = api.getRoomMessages.mock.calls.length;
    const roomsCalls = api.getChatRooms.mock.calls.length;
    await hideFor(61000);
    expect(api.getRoomMessages.mock.calls.length).toBe(history);
    expect(api.getChatRooms.mock.calls.length).toBeGreaterThan(roomsCalls);
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
