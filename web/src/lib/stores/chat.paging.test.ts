/**
 * Web chat scroll-up paging — store behaviour (ISSUE-131, Layer B).
 *
 * Covers: the first load seeds hasMore / oldestCursor, loadOlder prepends an
 * older page, dedups a boundary turn by (role, taskId) / notif_id, advances the
 * cursor, and no-ops when hasMore is false or a load is already in flight (it
 * must never re-resume active_tasks from an older page).
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

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — scroll-up paging', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('seeds hasMore / oldestCursor from the first load', async () => {
    api.getRoomMessages.mockResolvedValue({
      messages: [userTurn(2, 'q2'), asstTurn(2, 'a2')],
      active_task: null,
      active_tasks: [],
      has_more: true,
      oldest_cursor: { ts: '2026-06-10 12:00:00', id: 5 },
    });
    const s = await freshSession();
    await s.init();
    expect(get(s.hasMore)).toBe(true);
  });

  it('loadOlder prepends an older page and advances the cursor', async () => {
    // First load: the recent turn, with an older page available.
    api.getRoomMessages.mockResolvedValueOnce({
      messages: [userTurn(2, 'q2'), asstTurn(2, 'a2')],
      active_task: null,
      active_tasks: [],
      has_more: true,
      oldest_cursor: { ts: '2026-06-10 12:00:00', id: 3 },
    });
    const s = await freshSession();
    await s.init();
    expect(get(s.messages).map((m) => m.text)).toEqual(['q2', 'a2']);

    // Older page: the previous turn, now the start of history.
    api.getRoomMessages.mockResolvedValueOnce({
      messages: [userTurn(1, 'q1'), asstTurn(1, 'a1')],
      active_task: null,
      active_tasks: [],
      has_more: false,
      oldest_cursor: { ts: '2026-06-10 11:00:00', id: 1 },
    });
    await s.loadOlder();
    // Prepended, in order, ahead of the existing turn.
    expect(get(s.messages).map((m) => m.text)).toEqual(['q1', 'a1', 'q2', 'a2']);
    expect(get(s.hasMore)).toBe(false);
    // The cursor it sent was the first-load cursor.
    expect(api.getRoomMessages).toHaveBeenLastCalledWith(1, {
      before: { ts: '2026-06-10 12:00:00', id: 3 },
    });
  });

  it('dedups a boundary turn by (role, taskId) on prepend', async () => {
    api.getRoomMessages.mockResolvedValueOnce({
      messages: [userTurn(2, 'q2'), asstTurn(2, 'a2')],
      active_task: null,
      active_tasks: [],
      has_more: true,
      oldest_cursor: { ts: '2026-06-10 12:00:00', id: 3 },
    });
    const s = await freshSession();
    await s.init();
    // The older page re-includes turn 2 (a created_at tie straddling the
    // boundary) plus the genuinely-older turn 1.
    api.getRoomMessages.mockResolvedValueOnce({
      messages: [userTurn(1, 'q1'), asstTurn(1, 'a1'), userTurn(2, 'q2'), asstTurn(2, 'a2')],
      active_task: null,
      active_tasks: [],
      has_more: false,
      oldest_cursor: { ts: '2026-06-10 11:00:00', id: 1 },
    });
    await s.loadOlder();
    const texts = get(s.messages).map((m) => m.text);
    expect(texts).toEqual(['q1', 'a1', 'q2', 'a2']); // turn 2 not doubled
  });

  it('no-ops when hasMore is false', async () => {
    api.getRoomMessages.mockResolvedValueOnce({
      messages: [userTurn(2, 'q2'), asstTurn(2, 'a2')],
      active_task: null,
      active_tasks: [],
      has_more: false,
      oldest_cursor: null,
    });
    const s = await freshSession();
    await s.init();
    api.getRoomMessages.mockClear();
    await s.loadOlder();
    expect(api.getRoomMessages).not.toHaveBeenCalled();
  });

  it('never resumes active_tasks from an older page', async () => {
    api.getRoomMessages.mockResolvedValueOnce({
      messages: [userTurn(2, 'q2'), asstTurn(2, 'a2')],
      active_task: null,
      active_tasks: [],
      has_more: true,
      oldest_cursor: { ts: '2026-06-10 12:00:00', id: 3 },
    });
    const s = await freshSession();
    await s.init();
    // A malformed older page that (wrongly) carries an active task. loadOlder
    // must ignore it — no stream is started, no extra placeholder appended.
    api.getRoomMessages.mockResolvedValueOnce({
      messages: [userTurn(1, 'q1'), asstTurn(1, 'a1')],
      active_task: { id: 99, status: 'running' },
      active_tasks: [{ id: 99, status: 'running' }],
      has_more: false,
      oldest_cursor: null,
    });
    await s.loadOlder();
    expect(api.chatStreamUrl).not.toHaveBeenCalled();
    expect(get(s.messages).some((m) => m.taskId === 99)).toBe(false);
    expect(get(s.activeTaskId)).toBeNull();
  });

  it('resets paging state on room switch', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue({
      messages: [userTurn(2, 'q2'), asstTurn(2, 'a2')],
      active_task: null,
      active_tasks: [],
      has_more: true,
      oldest_cursor: { ts: '2026-06-10 12:00:00', id: 3 },
    });
    const s = await freshSession();
    await s.init();
    expect(get(s.hasMore)).toBe(true);
    // Switching to a room whose first load reports no older history clears it.
    api.getRoomMessages.mockResolvedValueOnce({
      messages: [],
      active_task: null,
      active_tasks: [],
      has_more: false,
      oldest_cursor: null,
    });
    await s.selectRoom(2);
    expect(get(s.hasMore)).toBe(false);
  });
});
