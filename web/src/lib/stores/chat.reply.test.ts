/**
 * Web chat reply-to-a-message — store behaviour.
 *
 * Covers: a reply send posts `reply_to_msg_id`; a retry preserves it; a 404
 * classifies as `reply_target_gone`, removes the row and hands the text back;
 * `buildHistoryMessage` maps the server's `reply_to` onto the row; and
 * `jumpToMsgId` resolves a canonical id, paging back for it.
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
function userTurn(
  taskId: number,
  text: string,
  over: Partial<ChatHistory['messages'][number]> = {},
): ChatHistory['messages'][number] {
  return {
    role: 'user',
    text,
    task_id: taskId,
    msg_id: taskId * 10 + 1,
    created_at: '2026-08-05T12:00:00Z',
    ...over,
  };
}
function asstTurn(taskId: number, text: string): ChatHistory['messages'][number] {
  return {
    role: 'assistant',
    text,
    task_id: taskId,
    msg_id: taskId * 10 + 2,
    status: 'completed',
    created_at: '2026-08-05T12:00:01Z',
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

// `vi.resetModules()` gives the chat module a fresh copy of every module it
// imports, notices included — so a statically imported `currentNotice` would be
// a different singleton from the one chat writes to.
async function freshNotices() {
  return await import('./notices');
}

describe('chat store — reply to a message', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatStreamUrl.mockReturnValue('/stream');
    api.getTaskEvents.mockResolvedValue({ events: [] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('posts reply_to_msg_id and stamps the citation on the optimistic row', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([userTurn(2, 'q2'), asstTurn(2, 'a2')]));
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 7 });
    const s = await freshSession();
    await s.init();

    await s.send('no, the second one', [], { msgId: 22, role: 'assistant', excerpt: 'a2' });

    const opts = api.sendChatMessage.mock.calls[0].at(-1);
    expect(opts).toMatchObject({ replyToMsgId: 22 });
    const row = get(s.messages).find((m) => m.taskId === 7 && m.role === 'user')!;
    expect(row.replyTo).toMatchObject({ msgId: 22, role: 'assistant' });
  });

  it('a plain send posts no citation', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([]));
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 7 });
    const s = await freshSession();
    await s.init();

    await s.send('hello');
    const opts = api.sendChatMessage.mock.calls[0].at(-1);
    expect(opts?.replyToMsgId).toBeUndefined();
  });

  it('retrySend re-POSTs the same citation', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([]));
    api.sendChatMessage.mockResolvedValueOnce({
      ok: false,
      status: 500,
      failure: 'rejected',
      error: 'boom',
    });
    const s = await freshSession();
    await s.init();

    await s.send('yes, do that', [], { msgId: 22, role: 'assistant', excerpt: 'a2' });
    const failed = get(s.messages).find((m) => m.sendState === 'failed')!;
    expect(failed).toBeTruthy();

    api.sendChatMessage.mockResolvedValueOnce({ ok: true, status: 200, task_id: 9 });
    await s.retrySend(failed.cid);
    const opts = api.sendChatMessage.mock.calls[1].at(-1);
    expect(opts).toMatchObject({ replyToMsgId: 22 });
  });

  it('a dead parent removes the row and hands the text back', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([]));
    api.sendChatMessage.mockResolvedValue({
      ok: false,
      status: 404,
      failure: 'reply_target_gone',
      error: 'the message you replied to is no longer available',
    });
    const s = await freshSession();
    await s.init();

    await s.send('yes, do that', [], { msgId: 22, role: 'assistant', excerpt: 'a2' });

    // The optimistic row is gone rather than left as a permanently
    // un-retryable failure: a retry would re-POST the same dead parent.
    expect(get(s.messages).some((m) => m.text === 'yes, do that')).toBe(false);
    const returned = get(s.sendReturned);
    expect(returned.token).toBe('t1');
    expect(returned.text).toBe('yes, do that');
    expect(returned.n).toBe(1);
  });

  it('the hand-back names the room it was typed in', async () => {
    // Leaving a room is not gated on `busy`, so a 404 can land after a switch
    // — and the page must not refill room B's composer with room A's text.
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue(page([]));
    api.sendChatMessage.mockResolvedValue({
      ok: false,
      status: 404,
      failure: 'reply_target_gone',
    });
    const s = await freshSession();
    await s.init();

    await s.send('yes, do that', [], { msgId: 22 });
    await s.selectRoom(2);
    expect(get(s.sendReturned).token).toBe('t1');
    expect(get(s.activeRoomId)).toBe(2);
  });

  it('an ordinary send failure still leaves its failed row', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([]));
    api.sendChatMessage.mockResolvedValue({
      ok: false,
      status: 500,
      failure: 'rejected',
      error: 'boom',
    });
    const s = await freshSession();
    await s.init();

    await s.send('hello');
    expect(get(s.messages).some((m) => m.sendState === 'failed')).toBe(true);
    expect(get(s.sendReturned).n).toBe(0);
  });

  it('maps the server citation onto a history row, deleted parent included', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(
      page([
        userTurn(2, 'live parent', {
          reply_to: { msg_id: 11, role: 'assistant', excerpt: 'earlier', deleted: false },
        }),
        userTurn(3, 'dead parent', {
          reply_to: { msg_id: 12, deleted: true },
        }),
      ]),
    );
    const s = await freshSession();
    await s.init();

    const live = get(s.messages).find((m) => m.taskId === 2)!;
    expect(live.replyTo).toEqual({
      msgId: 11,
      role: 'assistant',
      excerpt: 'earlier',
      deleted: false,
    });
    const dead = get(s.messages).find((m) => m.taskId === 3)!;
    expect(dead.replyTo).toEqual({ msgId: 12, deleted: true });
  });

  it('jumpToMsgId scrolls to a canonical id in the window', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([userTurn(2, 'q2'), asstTurn(2, 'a2')]));
    const s = await freshSession();
    await s.init();

    const target = get(s.messages).find((m) => m.msgId === 22)!;
    expect(await s.jumpToMsgId('t1', 22)).toBe(true);
    expect(get(s.scrollTarget)?.cid).toBe(target.cid);
  });

  it('jumpToMsgId pages older history to reach an off-window parent', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValueOnce(
      page([userTurn(5, 'q5'), asstTurn(5, 'a5')], {
        has_more: true,
        oldest_cursor: { ts: '2026-08-05 12:00:00', id: 51 },
      }),
    );
    const s = await freshSession();
    await s.init();
    expect(get(s.messages).some((m) => m.msgId === 32)).toBe(false);

    api.getRoomMessages.mockResolvedValueOnce(
      page([userTurn(3, 'q3'), asstTurn(3, 'a3')], { has_more: false, oldest_cursor: null }),
    );
    expect(await s.jumpToMsgId('t1', 32)).toBe(true);
    const target = get(s.messages).find((m) => m.msgId === 32)!;
    expect(get(s.scrollTarget)?.cid).toBe(target.cid);
  });

  it('jumpToMsgId returns false and says so when the parent is unreachable', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(page([userTurn(2, 'q2'), asstTurn(2, 'a2')]));
    const s = await freshSession();
    const notices = await freshNotices();
    await s.init();
    notices.clearNotices();

    expect(await s.jumpToMsgId('t1', 999)).toBe(false);
    // Silence would read as a dead button.
    expect(get(notices.currentNotice)?.message).toContain('locate that message');
  });
});
