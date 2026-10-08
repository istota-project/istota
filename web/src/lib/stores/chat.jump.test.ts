/**
 * Web chat jump-to-response — store behaviour (memory-search overhaul, Stage 5).
 *
 * Covers: jumpToTask resolves a task's transcript cid and signals scrollTarget;
 * selects a different room first; pages older history to find an off-window
 * turn; degrades gracefully (returns false, no scroll) on unknown room / not
 * found; scrollToCid bumps the nonce.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

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
function userTurn(taskId: number, text: string): ChatHistory['messages'][number] {
  return { role: 'user', text, task_id: taskId, created_at: '2026-06-10T12:00:00Z' };
}
function asstTurn(taskId: number, text: string): ChatHistory['messages'][number] {
  return {
    role: 'assistant',
    text,
    task_id: taskId,
    status: 'completed',
    created_at: '2026-06-10T12:00:01Z',
    segments: [{ kind: 'text', text }],
  };
}
function page(msgs: ChatHistory['messages'], over: Partial<ChatHistory> = {}): ChatHistory {
  return {
    messages: msgs,
    active_task: null,
    active_tasks: [],
    has_more: false,
    oldest_cursor: null,
    ...over,
  } as ChatHistory;
}

// `vi.resetModules()` gives the chat module a fresh copy of every module it
// imports, notices included — so a statically imported `currentNotice` would be
// a different singleton from the one chat writes to.
async function freshNotices() {
  return await import('./notices');
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — jump-to-response', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('resolves an in-window task to its assistant cid and signals scrollTarget', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([userTurn(2, 'q2'), asstTurn(2, 'a2')]));
    const s = await freshSession();
    await s.init();

    const asst = get(s.messages).find((m) => m.taskId === 2 && m.role === 'assistant')!;
    const ok = await s.jumpToTask('t1', 2);
    expect(ok).toBe(true);
    expect(get(s.scrollTarget)?.cid).toBe(asst.cid);
  });

  it('selects a different room before resolving the turn', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockImplementation((roomId: number) =>
      roomId === 2
        ? Promise.resolve(page([userTurn(9, 'q9'), asstTurn(9, 'a9')]))
        : Promise.resolve(page([userTurn(2, 'q2'), asstTurn(2, 'a2')])),
    );
    const s = await freshSession();
    await s.init();
    // Starts in room 1.
    expect(get(s.activeRoomId)).toBe(1);

    const ok = await s.jumpToTask('t2', 9);
    expect(ok).toBe(true);
    expect(get(s.activeRoomId)).toBe(2);
    const asst = get(s.messages).find((m) => m.taskId === 9 && m.role === 'assistant')!;
    expect(get(s.scrollTarget)?.cid).toBe(asst.cid);
  });

  it('pages older history to reach an off-window turn', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    // First load: recent turn, older page available.
    api.getRoomMessages.mockResolvedValueOnce(
      page([userTurn(5, 'q5'), asstTurn(5, 'a5')], {
        has_more: true,
        oldest_cursor: { ts: '2026-06-10 12:00:00', id: 5 },
      }),
    );
    const s = await freshSession();
    await s.init();
    expect(get(s.messages).some((m) => m.taskId === 3)).toBe(false);

    // The older page carries the target turn 3.
    api.getRoomMessages.mockResolvedValueOnce(
      page([userTurn(3, 'q3'), asstTurn(3, 'a3')], { has_more: false, oldest_cursor: null }),
    );
    const ok = await s.jumpToTask('t1', 3);
    expect(ok).toBe(true);
    const asst = get(s.messages).find((m) => m.taskId === 3 && m.role === 'assistant')!;
    expect(get(s.scrollTarget)?.cid).toBe(asst.cid);
  });

  it('returns false and raises a notice for an unknown room token', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([userTurn(2, 'q2'), asstTurn(2, 'a2')]));
    const s = await freshSession();
    const notices = await freshNotices();
    await s.init();
    notices.clearNotices();

    const ok = await s.jumpToTask('t-unknown', 2);
    expect(ok).toBe(false);
    // A dead deep link has nowhere inline to report itself — the transcript it
    // named is the thing that isn't there.
    expect(get(notices.currentNotice)?.message).toBe("Couldn't open that conversation.");
    expect(get(s.scrollTarget)).toBeNull();
  });

  it('returns false when the task is not found after paging is exhausted', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(
      page([userTurn(2, 'q2'), asstTurn(2, 'a2')], { has_more: false, oldest_cursor: null }),
    );
    const s = await freshSession();
    await s.init();

    const ok = await s.jumpToTask('t1', 999);
    expect(ok).toBe(false);
    expect(get(s.scrollTarget)).toBeNull();
  });

  it('scrollToCid bumps the nonce so a repeat jump re-fires', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([userTurn(2, 'q2'), asstTurn(2, 'a2')]));
    const s = await freshSession();
    await s.init();

    s.scrollToCid(7);
    const first = get(s.scrollTarget)!;
    s.scrollToCid(7);
    const second = get(s.scrollTarget)!;
    expect(first.cid).toBe(7);
    expect(second.cid).toBe(7);
    expect(second.nonce).toBeGreaterThan(first.nonce);
  });
  it.each([false, true])('fetches one contiguous cursor band (truncated=%s)', async (truncated) => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    const before = { ts: '2026-06-10 12:00:00', id: 900 };
    const until = { ts: '2026-01-01 00:00:00', id: 1 };
    api.getRoomMessages.mockResolvedValueOnce(
      page([{ ...asstTurn(900, 'recent'), msg_id: 900 }], {
        has_more: true,
        oldest_cursor: before,
      }),
    );
    const s = await freshSession();
    const notices = await freshNotices();
    await s.init();
    notices.clearNotices();
    api.getRoomMessages.mockClear();
    api.getRoomMessages.mockResolvedValueOnce(
      page([{ ...asstTurn(1, 'old'), msg_id: truncated ? 2 : 1 }], {
        has_more: true,
        oldest_cursor: until,
        truncated,
      }),
    );
    expect(await s.jumpToMsgId('t1', 1, until)).toBe(!truncated);
    expect(api.getRoomMessages).toHaveBeenCalledExactlyOnceWith(1, { before, until });
    expect(get(s.messages)).toHaveLength(2);
    if (truncated)
      expect(get(notices.currentNotice)?.message).toBe(
        'That message is too far back to open here.',
      );
    else expect(get(s.scrollTarget)?.cid).toBe(get(s.messages)[0].cid);
  });

  it.each([false, true])(
    'fills a system-note hole without moving the paging cursor (more=%s)',
    async (more) => {
      api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
      const oldest = { ts: '2026-06-10 12:00:00', id: 1 };
      const target = { ts: '2026-06-10 12:05:00', id: 2 };
      const next = { ts: target.ts, id: target.id + 1 };
      api.getRoomMessages.mockResolvedValueOnce(
        page(
          [
            { ...userTurn(1, 'first'), created_at: '2026-06-10T12:00:00Z', msg_id: 1 },
            {
              role: 'system',
              text: 'last visible note',
              created_at: '2026-06-10T12:05:00Z',
              msg_id: 12,
              notif_id: 12,
            },
            { ...asstTurn(3, 'last'), created_at: '2026-06-10T12:09:00Z', msg_id: 63 },
          ],
          { oldest_cursor: oldest, has_more: more },
        ),
      );
      const session = await freshSession();
      await session.init();
      api.getRoomMessages.mockClear();
      api.getRoomMessages.mockResolvedValueOnce(
        page(
          [
            {
              role: 'system',
              text: 'omitted note',
              created_at: '2026-06-10T12:05:00Z',
              msg_id: 2,
              notif_id: 2,
            },
          ],
          { oldest_cursor: target, has_more: true },
        ),
      );
      expect(await session.jumpToMsgId('t1', 2, target)).toBe(true);
      expect(api.getRoomMessages).toHaveBeenCalledExactlyOnceWith(1, {
        before: next,
        until: target,
      });
      expect(get(session.messages).map((m) => m.msgId)).toEqual([1, 2, 12, 63]);
      expect(get(session.hasMore)).toBe(more);
      if (more) {
        api.getRoomMessages.mockResolvedValueOnce(page([]));
        await session.loadOlder();
        expect(api.getRoomMessages).toHaveBeenLastCalledWith(1, { before: oldest });
      }
    },
  );

  it('keeps the five-page bound without a cursor', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(
      page([asstTurn(2, 'recent')], {
        has_more: true,
        oldest_cursor: { ts: '2026-06-10 12:00:00', id: 2 },
      }),
    );
    const s = await freshSession();
    await s.init();
    api.getRoomMessages.mockClear();
    expect(await s.jumpToMsgId('t1', 999)).toBe(false);
    expect(api.getRoomMessages).toHaveBeenCalledTimes(5);
  });
  it.each(['missing', 'failed'])(
    'reports a %s cursor jump without paging again',
    async (outcome) => {
      api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
      const cursor = { ts: '2026-01-01 00:00:00', id: 1 };
      api.getRoomMessages.mockResolvedValueOnce(
        page([{ ...asstTurn(2, 'recent'), msg_id: 2 }], {
          has_more: true,
          oldest_cursor: { ts: '2026-06-10 12:00:00', id: 2 },
        }),
      );
      const s = await freshSession();
      const notices = await freshNotices();
      await s.init();
      notices.clearNotices();
      api.getRoomMessages.mockClear();
      if (outcome === 'failed') api.getRoomMessages.mockRejectedValueOnce(new Error('offline'));
      else api.getRoomMessages.mockResolvedValueOnce(page([]));
      expect(await s.jumpToMsgId('t1', 1, cursor)).toBe(false);
      expect(api.getRoomMessages).toHaveBeenCalledTimes(1);
      expect(get(notices.currentNotice)?.message).toBe(
        outcome === 'failed' ? "Couldn't jump to that message." : "Couldn't locate that message.",
      );
      expect(get(s.activeRoomId)).toBe(1);
    },
  );
});
