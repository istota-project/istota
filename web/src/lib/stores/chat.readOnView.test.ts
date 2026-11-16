/**
 * Read-on-view in the aggregate panes — store behaviour (web-chat-read-on-view
 * spec, stage 4).
 *
 * Covers the pending set and its flush: `noteSeen` debounced into one POST,
 * the response applied to badges, markers and the set, resends of ids that
 * could not advance, the in-flight latch, the 500-id cap, the flush on leaving
 * the pane, the preference off, the Unread page limit and its open-at-top
 * signal, and `unread` on built and streamed rows.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory, ReadSeenResponse } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
  chatRoomStreamUrl: vi.fn(() => '/stream'),
  chatStreamUrl: vi.fn(() => '/task-stream'),
});

const invalidateNavUnreadCounts = vi.hoisted(() => vi.fn());
vi.mock('./navUnread', () => ({ invalidateNavUnreadCounts }));

vi.mock('@minot/ui/ui/persisted', () => ({
  loadSetting: vi.fn(() => null),
  saveSetting: vi.fn(),
}));

type Row = ChatHistory['messages'][number];

function room(id: number, unread = 0): ChatRoom {
  return {
    id,
    token: `t${id}`,
    name: `Room ${id}`,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: unread,
  };
}

function row(msgId: number, token: string, over: Partial<Row> = {}): Row {
  return {
    role: 'assistant',
    text: `msg ${msgId}`,
    msg_id: msgId,
    starred: false,
    task_id: msgId,
    status: 'completed',
    room_token: token,
    room_name: token,
    created_at: '2026-11-16T10:00:00Z',
    segments: [{ kind: 'text', text: `msg ${msgId}` }],
    unread: true,
    ...over,
  };
}

function seenResponse(...rooms: ReadSeenResponse['rooms']): ReadSeenResponse {
  return { rooms };
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

type Session = Awaited<ReturnType<typeof freshSession>>;

async function openView(
  view: 'all' | 'unread' | 'starred',
  rows: Row[],
  // null: a config without the key, as an older backend sends.
  readOnView: boolean | null = true,
): Promise<Session> {
  api.getChatConfig.mockResolvedValue({
    client_poll_interval_ms: 1500,
    ...(readOnView === null ? {} : { chat_read_on_view: readOnView }),
  });
  api.getChatMessagesView.mockResolvedValue({
    messages: rows,
    has_more: false,
    oldest_cursor: null,
  });
  const s = await freshSession();
  await s.init();
  await s.selectView(view);
  // Opening the first room on init marks it read; only this stage's calls count.
  invalidateNavUnreadCounts.mockClear();
  return s;
}

function cidOf(s: Session, msgId: number): number {
  const m = get(s.messages).find((x) => x.msgId === msgId);
  if (!m) throw new Error(`no row ${msgId}`);
  return m.cid;
}

function unreadOf(s: Session, msgId: number): boolean | undefined {
  return get(s.messages).find((x) => x.msgId === msgId)?.unread;
}

function sent(call: number): number[] {
  return api.markMessagesSeen.mock.calls[call][0] as number[];
}

describe('chat store — read on view', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    invalidateNavUnreadCounts.mockClear();
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.chatRoomStreamUrl.mockReturnValue('/stream');
    api.getChatRooms.mockResolvedValue({ rooms: [room(1, 3), room(2, 2)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.markMessagesSeen.mockResolvedValue(seenResponse());
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('posts the pending ids once, 1 s after the last one joins', async () => {
    const s = await openView('unread', [row(10, 't1'), row(11, 't2'), row(12, 't1')]);
    s.noteSeen(cidOf(s, 10));
    await vi.advanceTimersByTimeAsync(600);
    s.noteSeen(cidOf(s, 12));
    s.noteSeen(cidOf(s, 11));
    await vi.advanceTimersByTimeAsync(600);
    expect(api.markMessagesSeen).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(400);
    expect(api.markMessagesSeen).toHaveBeenCalledTimes(1);
    expect(sent(0)).toEqual([10, 11, 12]);
    s.teardown();
  });

  it('applies the response to badges, markers and the pending set, room by room', async () => {
    const s = await openView('unread', [row(9, 't2'), row(10, 't1'), row(12, 't1'), row(15, 't1')]);
    api.markMessagesSeen.mockResolvedValueOnce(
      seenResponse({ token: 't1', last_read_message_id: 12, unread_count: 1, advanced: true }),
    );
    s.noteSeen(cidOf(s, 10));
    s.noteSeen(cidOf(s, 12));
    await vi.advanceTimersByTimeAsync(1000);
    expect(get(s.rooms).find((r) => r.token === 't1')?.unread_count).toBe(1);
    expect(get(s.rooms).find((r) => r.token === 't2')?.unread_count).toBe(2);
    expect(unreadOf(s, 10)).toBe(false);
    expect(unreadOf(s, 12)).toBe(false);
    expect(unreadOf(s, 15)).toBe(true);
    // Below t1's cursor by id, but another room's row: untouched.
    expect(unreadOf(s, 9)).toBe(true);
    expect(invalidateNavUnreadCounts).toHaveBeenCalledTimes(1);
    // 10 and 12 left the pending set with the cursor that passed them.
    s.noteSeen(cidOf(s, 15));
    await vi.advanceTimersByTimeAsync(1000);
    expect(sent(1)).toEqual([15]);
    s.teardown();
  });

  it('resends ids that did not advance, and ids from a failed flush', async () => {
    const s = await openView('all', [row(10, 't1'), row(12, 't1'), row(15, 't1')]);
    api.markMessagesSeen.mockResolvedValueOnce(
      seenResponse({ token: 't1', last_read_message_id: 5, unread_count: 3, advanced: false }),
    );
    s.noteSeen(cidOf(s, 15));
    await vi.advanceTimersByTimeAsync(1000);
    expect(sent(0)).toEqual([15]);
    expect(unreadOf(s, 15)).toBe(true);
    expect(invalidateNavUnreadCounts).not.toHaveBeenCalled();

    api.markMessagesSeen.mockRejectedValueOnce(new Error('offline'));
    s.noteSeen(cidOf(s, 12));
    await vi.advanceTimersByTimeAsync(1000);
    expect(sent(1)).toEqual([12, 15]);

    s.noteSeen(cidOf(s, 10));
    await vi.advanceTimersByTimeAsync(1000);
    expect(sent(2)).toEqual([10, 12, 15]);
    s.teardown();
  });

  it('keeps one request in flight and sends what joined meanwhile after it', async () => {
    const s = await openView('unread', [row(10, 't1'), row(12, 't1')]);
    let answer: (r: ReadSeenResponse) => void = () => {};
    api.markMessagesSeen.mockImplementationOnce(
      () => new Promise<ReadSeenResponse>((resolve) => (answer = resolve)),
    );
    s.noteSeen(cidOf(s, 10));
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.markMessagesSeen).toHaveBeenCalledTimes(1);
    s.noteSeen(cidOf(s, 12));
    await vi.advanceTimersByTimeAsync(3000);
    expect(api.markMessagesSeen).toHaveBeenCalledTimes(1);
    answer(
      seenResponse({ token: 't1', last_read_message_id: 10, unread_count: 1, advanced: true }),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(api.markMessagesSeen).toHaveBeenCalledTimes(2);
    expect(sent(1)).toEqual([12]);
    s.teardown();
  });

  it('bounds the flush it waits on, so a stalled request cannot hold the latch', async () => {
    const s = await openView('unread', [row(10, 't1')]);
    s.noteSeen(cidOf(s, 10));
    await vi.advanceTimersByTimeAsync(1000);
    const [, opts] = api.markMessagesSeen.mock.calls[0];
    expect(opts.timeoutMs).toBeGreaterThan(0);
    expect(opts.keepalive).toBeUndefined();
    s.teardown();
  });

  it('does not let an older response put a badge back up', async () => {
    const s = await openView('unread', [row(10, 't1'), row(12, 't1')]);
    let answerOld: (r: ReadSeenResponse) => void = () => {};
    api.markMessagesSeen.mockImplementationOnce(
      () => new Promise<ReadSeenResponse>((resolve) => (answerOld = resolve)),
    );
    s.noteSeen(cidOf(s, 10));
    await vi.advanceTimersByTimeAsync(1000);
    api.markMessagesSeen.mockResolvedValueOnce(
      seenResponse({ token: 't1', last_read_message_id: 12, unread_count: 0, advanced: true }),
    );
    s.noteSeen(cidOf(s, 12));
    s.flushSeen({ keepalive: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(get(s.rooms).find((r) => r.token === 't1')?.unread_count).toBe(0);
    answerOld(
      seenResponse({ token: 't1', last_read_message_id: 10, unread_count: 2, advanced: true }),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(get(s.rooms).find((r) => r.token === 't1')?.unread_count).toBe(0);
    s.teardown();
  });

  it('clears every unread marker on mark all read', async () => {
    api.markAllRoomsRead.mockResolvedValue({ ok: true, updated: 2 });
    const s = await openView('all', [row(10, 't1'), row(11, 't2')]);
    await s.markAllRead();
    expect(unreadOf(s, 10)).toBe(false);
    expect(unreadOf(s, 11)).toBe(false);
    s.teardown();
  });

  it('sends the lowest 500 ids when more are pending', async () => {
    const rows = Array.from({ length: 600 }, (_, i) => row(i + 1, 't1'));
    const s = await openView('unread', rows);
    for (let id = 600; id >= 1; id--) s.noteSeen(cidOf(s, id));
    await vi.advanceTimersByTimeAsync(1000);
    expect(sent(0)).toEqual(Array.from({ length: 500 }, (_, i) => i + 1));
    s.teardown();
  });

  it.each(['view', 'room', 'teardown'] as const)(
    'flushes with keepalive on leaving the pane (%s), then drops the set',
    async (leave) => {
      api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
      const s = await openView('unread', [row(10, 't1'), row(12, 't1')]);
      s.noteSeen(cidOf(s, 12));
      s.noteSeen(cidOf(s, 10));
      if (leave === 'view') await s.selectView('all');
      else if (leave === 'room') await s.selectRoom(2);
      else s.teardown();
      expect(api.markMessagesSeen).toHaveBeenCalledTimes(1);
      expect(api.markMessagesSeen).toHaveBeenCalledWith([10, 12], { keepalive: true });
      await vi.advanceTimersByTimeAsync(3000);
      expect(api.markMessagesSeen).toHaveBeenCalledTimes(1);
      s.teardown();
    },
  );

  it('flushes at once with keepalive on request, keeping the set for the pane', async () => {
    const s = await openView('unread', [row(10, 't1')]);
    s.noteSeen(cidOf(s, 10));
    s.flushSeen({ keepalive: true });
    expect(api.markMessagesSeen).toHaveBeenCalledWith([10], { keepalive: true });
    await vi.advanceTimersByTimeAsync(0);
    // Nothing advanced, so the id is still pending and goes with the next.
    s.flushSeen();
    expect(sent(1)).toEqual([10]);
    s.teardown();
  });

  it('ignores rows that are not unread', async () => {
    const s = await openView('all', [
      row(10, 't1', { unread: false }),
      row(11, 't1', { role: 'user', unread: false }),
    ]);
    s.noteSeen(cidOf(s, 10));
    s.noteSeen(cidOf(s, 11));
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.markMessagesSeen).not.toHaveBeenCalled();
    s.teardown();
  });

  it('opens Unread with the large page and a scroll to its first row', async () => {
    const s = await openView('unread', [row(10, 't1'), row(12, 't1')]);
    expect(get(s.readOnView)).toBe(true);
    expect(api.getChatMessagesView).toHaveBeenLastCalledWith('unread', { limit: 200 });
    expect(get(s.scrollTarget)).toMatchObject({ cid: cidOf(s, 10), block: 'start' });
    s.teardown();
  });

  it('keeps All at the newest row with the default page', async () => {
    const s = await openView('all', [row(10, 't1')]);
    expect(api.getChatMessagesView).toHaveBeenLastCalledWith('all');
    expect(get(s.scrollTarget)).toBeNull();
    s.teardown();
  });

  it.each([false, null])('does nothing with the preference off (%s)', async (pref) => {
    const s = await openView('unread', [row(10, 't1')], pref);
    expect(get(s.readOnView)).toBe(false);
    expect(api.getChatMessagesView).toHaveBeenLastCalledWith('unread');
    expect(get(s.scrollTarget)).toBeNull();
    s.noteSeen(cidOf(s, 10));
    await vi.advanceTimersByTimeAsync(2000);
    s.flushSeen({ keepalive: true });
    expect(api.markMessagesSeen).not.toHaveBeenCalled();
    s.teardown();
  });

  it('marks a streamed non-user row unread in the aggregate pane, and a user row not', async () => {
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 5, gap: false });
    const s = await openView('all', []);
    const { unread: _drop, ...bare } = row(20, 't2');
    void _drop;
    api.getRoomEvents.mockResolvedValueOnce({
      events: [bare, { ...bare, msg_id: 21, task_id: 21, role: 'user', text: 'mine' }],
      cursor: 21,
      gap: false,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 21, gap: false });
    await vi.advanceTimersByTimeAsync(2000);
    expect(unreadOf(s, 20)).toBe(true);
    expect(unreadOf(s, 21)).toBe(false);
    s.teardown();
  });
});
