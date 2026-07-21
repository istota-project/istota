/**
 * Web chat jump-to-response — store behaviour (memory-search overhaul, Stage 5).
 *
 * Covers: jumpToTask resolves a task's transcript cid and signals scrollTarget;
 * selects a different room first; pages older history to find an off-window
 * turn; degrades gracefully (returns false, no scroll) on unknown room / not
 * found; scrollToCid bumps the nonce.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory } from '$lib/api';

const api = vi.hoisted(() => ({
  getChatConfig: vi.fn(),
  getChatRooms: vi.fn(),
  getRoomMessages: vi.fn(),
  markRoomRead: vi.fn(),
  getTaskEvents: vi.fn(),
  sendChatMessage: vi.fn(),
  createChatRoom: vi.fn(),
  updateChatRoom: vi.fn(),
  deleteChatRoom: vi.fn(),
  promoteChatRoom: vi.fn(),
  cancelChatTask: vi.fn(),
  confirmChatTask: vi.fn(),
  chatStreamUrl: vi.fn(),
  ChatRoomBusyError: class extends Error {},
}));

vi.mock('$lib/api', () => api);
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

  it('returns false and sets an error for an unknown room token', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([userTurn(2, 'q2'), asstTurn(2, 'a2')]));
    const s = await freshSession();
    await s.init();

    const ok = await s.jumpToTask('t-unknown', 2);
    expect(ok).toBe(false);
    expect(get(s.error)).not.toBe('');
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
});
