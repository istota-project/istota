/**
 * Cancelling a turn that has been sent but has no task id yet.
 *
 * The composer shows Stop from the moment `send` sets 'sending', which is
 * before the POST returns — so on a slow connection there is a real window in
 * which a tap on Stop has nothing to target. It used to return silently, which
 * read to the user as a dead button.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom } from '$lib/api';

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

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — cancel', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [], next_seq: 0 });
    api.cancelChatTask.mockResolvedValue({ ok: true });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('applies a Stop tapped before the task id arrived', async () => {
    const s = await freshSession();
    await s.init();

    let resolveSend: (v: unknown) => void = () => {};
    api.sendChatMessage.mockReturnValue(
      new Promise((r) => {
        resolveSend = r;
      }),
    );
    const sending = s.send('hello');
    await Promise.resolve();
    expect(get(s.status)).toBe('sending');

    // Tapped while the POST is still open: nothing to cancel yet.
    await s.cancel();
    expect(api.cancelChatTask).not.toHaveBeenCalled();

    resolveSend({ ok: true, task_id: 42 });
    await sending;
    expect(api.cancelChatTask).toHaveBeenCalledWith(42);
  });

  it('does not arm a later turn from a cancel with nothing in flight', async () => {
    const s = await freshSession();
    await s.init();

    // Idle: this cancel must evaporate, not wait for the next send.
    await s.cancel();

    api.sendChatMessage.mockResolvedValue({ ok: true, task_id: 7 });
    await s.send('hello');
    expect(api.cancelChatTask).not.toHaveBeenCalled();
  });

  it('cancels the active task directly once it is streaming', async () => {
    const s = await freshSession();
    await s.init();
    api.sendChatMessage.mockResolvedValue({ ok: true, task_id: 9 });
    await s.send('hello');
    expect(get(s.activeTaskId)).toBe(9);

    await s.cancel();
    expect(api.cancelChatTask).toHaveBeenCalledWith(9);
  });
});
