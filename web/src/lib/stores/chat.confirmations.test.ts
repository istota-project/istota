/**
 * The pending-confirmations banner (ISSUE-241).
 *
 * An inbound email held by the untrusted-sender gate belongs to no room — its
 * conversation token is a synthetic thread hash — and its body is deliberately
 * withheld from every transcript until the user approves it. So there was
 * nothing in web chat to render a card on, and the user was never asked. This
 * store slice is the surface for those questions.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom, PendingConfirmation } from '$lib/api';

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
  listPendingConfirmations: vi.fn(),
  chatStreamUrl: vi.fn(),
  ChatRoomBusyError: class extends Error {},
}));

vi.mock('$lib/api', () => api);
vi.mock('$lib/stores/persisted', () => ({
  loadSetting: vi.fn(() => null),
  saveSetting: vi.fn(),
}));

const notices = vi.hoisted(() => ({
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('./notices', () => notices);

function room(id: number): ChatRoom {
  return {
    id,
    token: `t${id}`,
    name: `Room ${id}`,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: 0,
  };
}

function gate(taskId: number, sender = 'stranger@evil.com'): PendingConfirmation {
  return {
    task_id: taskId,
    source_type: 'email',
    created_at: '2026-01-01T10:00:00Z',
    prompt: `Email from unknown sender ${sender}`,
    summary: `email from ${sender} — Invite`,
    room_token: null,
    email: { sender, subject: 'Invite', routing_method: 'plus_address' },
  };
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — pending confirmations', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    notices.notifyError.mockReset();
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [], next_seq: 0 });
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('seeds the banner on entry, so a gate parked before the tab opened is visible', async () => {
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [gate(40122)] });
    const s = await freshSession();
    await s.init();
    await Promise.resolve();

    expect(api.listPendingConfirmations).toHaveBeenCalled();
    expect(get(s.pendingConfirmations).map((c) => c.task_id)).toEqual([40122]);
  });

  it('confirming clears the card and releases the task', async () => {
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [gate(1), gate(2)] });
    api.confirmChatTask.mockResolvedValue({ status: 'ok' });
    const s = await freshSession();
    await s.init();
    await s.refreshConfirmations();

    await s.answerConfirmation(1, true);

    expect(api.confirmChatTask).toHaveBeenCalledWith(1);
    expect(get(s.pendingConfirmations).map((c) => c.task_id)).toEqual([2]);
  });

  it('discarding cancels the task rather than confirming it', async () => {
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [gate(5)] });
    api.cancelChatTask.mockResolvedValue({ status: 'ok' });
    const s = await freshSession();
    await s.init();
    await s.refreshConfirmations();

    await s.answerConfirmation(5, false);

    expect(api.cancelChatTask).toHaveBeenCalledWith(5);
    expect(api.confirmChatTask).not.toHaveBeenCalled();
    expect(get(s.pendingConfirmations)).toEqual([]);
  });

  it('keeps the card when the answer fails, so the question is not lost', async () => {
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [gate(9)] });
    api.confirmChatTask.mockRejectedValue(new Error('offline'));
    const s = await freshSession();
    await s.init();
    await s.refreshConfirmations();

    await s.answerConfirmation(9, true);

    expect(get(s.pendingConfirmations).map((c) => c.task_id)).toEqual([9]);
    expect(notices.notifyError).toHaveBeenCalled();
  });

  it('a failed poll leaves the banner alone and raises no notice', async () => {
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [gate(3)] });
    const s = await freshSession();
    await s.init();
    await s.refreshConfirmations();
    expect(get(s.pendingConfirmations)).toHaveLength(1);

    api.listPendingConfirmations.mockRejectedValue(new Error('network'));
    await s.refreshConfirmations();

    expect(get(s.pendingConfirmations)).toHaveLength(1);
    expect(notices.notifyError).not.toHaveBeenCalled();
  });
});

/**
 * Answering with a bare "yes" from the composer (ISSUE-243).
 *
 * The endpoint runs it inside the request like a `!command` — no task, an
 * inline result — but unlike a command the exchange is *durable*: the server
 * writes the answer and the ack into `messages`, so both come back over the
 * room stream. The ids it returns are what stop that echo appending a second
 * copy of each.
 */
describe('chat store — answering a confirmation from the composer', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    notices.notifyError.mockReset();
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [], next_seq: 0 });
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  async function answeredSession() {
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [gate(40122)] });
    const s = await freshSession();
    await s.init();
    await s.refreshConfirmations();
    expect(get(s.pendingConfirmations)).toHaveLength(1);

    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'Confirmed.',
      command_data: { kind: 'confirmation_answered', user_msg_id: 71, system_msg_id: 72 },
    });
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [] });
    await s.send('yes');
    return s;
  }

  it('renders the ack and stamps both durable ids onto the rows', async () => {
    const s = await answeredSession();

    const rows = get(s.messages);
    const user = rows.find((m) => m.role === 'user');
    const ack = rows.find((m) => m.role === 'system');
    expect(user?.msgId).toBe(71);
    expect(ack?.text).toBe('Confirmed.');
    expect(ack?.msgId).toBe(72);
  });

  it('clears the banner immediately rather than at the next poll', async () => {
    const s = await answeredSession();
    expect(get(s.pendingConfirmations)).toEqual([]);
  });

  it('an ordinary inline command is untouched by the stamping', async () => {
    const s = await freshSession();
    await s.init();
    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'pong',
    });

    await s.send('!ping');

    const ack = get(s.messages).find((m) => m.role === 'system');
    expect(ack?.text).toBe('pong');
    expect(ack?.msgId).toBeUndefined();
  });
});
