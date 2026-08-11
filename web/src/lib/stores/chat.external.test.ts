/**
 * External-origin turns — store behaviour.
 *
 * Two halves. `buildHistoryMessage` is the single place a server row becomes a
 * transcript row, which is what keeps history, the aggregate panes and the live
 * stream from disagreeing about which turns are external; and
 * `externalTurnDisplay` is read from `/chat/config` rather than kept per
 * browser, because how much of a stranger's mail a reader wants inline is a
 * decision about the account.
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
    origin: 'talk',
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
    created_at: '2026-08-10T12:00:00Z',
    ...over,
  };
}

function page(msgs: ChatHistory['messages']): ChatHistory {
  return {
    messages: msgs,
    active_task: null,
    active_tasks: [],
    has_more: false,
    oldest_cursor: null,
  } as ChatHistory;
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — external-origin turns', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatStreamUrl.mockReturnValue('/stream');
    api.getTaskEvents.mockResolvedValue({ events: [] });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('carries origin and subject onto the row', async () => {
    api.getRoomMessages.mockResolvedValue(
      page([
        userTurn(2, 'does the west branch work?', {
          origin: 'email',
          subject: 'Re: Scheduling',
          author: 'contact@example.com',
        }),
      ]),
    );
    const s = await freshSession();
    await s.init();

    const row = get(s.messages).find((m) => m.role === 'user')!;
    expect(row.origin).toBe('email');
    expect(row.subject).toBe('Re: Scheduling');
    expect(row.author).toBe('contact@example.com');
  });

  it('leaves both undefined on an ordinary turn', async () => {
    // Absence is the signal the component keys on, so an empty string arriving
    // from an older payload must not read as an origin.
    api.getRoomMessages.mockResolvedValue(
      page([userTurn(2, 'hello'), userTurn(3, 'again', { origin: '', subject: '' })]),
    );
    const s = await freshSession();
    await s.init();

    for (const row of get(s.messages).filter((m) => m.role === 'user')) {
      expect(row.origin).toBeUndefined();
      expect(row.subject).toBeUndefined();
    }
  });

  it('adopts the reader’s display setting from /chat/config', async () => {
    api.getChatConfig.mockResolvedValue({
      client_poll_interval_ms: 1500,
      external_turn_display: 'full',
    });
    api.getRoomMessages.mockResolvedValue(page([]));
    const s = await freshSession();
    await s.init();

    expect(get(s.externalTurnDisplay)).toBe('full');
  });

  it('falls back to collapsed for a value the code does not recognize', async () => {
    // The column takes any string a hand edit puts in it, and the transcript
    // needs a branch it can take — the safe direction being less of a
    // stranger's text on screen, not more.
    api.getChatConfig.mockResolvedValue({
      client_poll_interval_ms: 1500,
      external_turn_display: 'sideways',
    });
    api.getRoomMessages.mockResolvedValue(page([]));
    const s = await freshSession();
    await s.init();

    expect(get(s.externalTurnDisplay)).toBe('collapsed');
  });

  it('holds the default when the config request fails outright', async () => {
    api.getChatConfig.mockRejectedValue(new Error('offline'));
    api.getRoomMessages.mockResolvedValue(page([]));
    const s = await freshSession();
    await s.init();

    expect(get(s.externalTurnDisplay)).toBe('collapsed');
  });
});
