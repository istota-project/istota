/**
 * ISSUE-401 — "Also open in Talk" / "Reconnect to Talk", store behaviour.
 *
 * The endpoint answers with a status because three of its five outcomes change
 * nothing, and one of those is the server saying the existing Talk link is
 * fine. The store used to discard the whole answer on failure and merge it
 * blindly on success, so a refused promote and a repaired one looked identical
 * from the app — which is what made a room with a dead binding read as a button
 * that does nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom } from '$lib/api';

const api = vi.hoisted(() => ({
  getChatConfig: vi.fn(),
  getChatRooms: vi.fn(),
  getRoomMessages: vi.fn(),
  getChatMessagesView: vi.fn(),
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
  chatStreamUrl: vi.fn(),
  ChatRoomBusyError: class extends Error {},
}));

vi.mock('$lib/api', () => api);
vi.mock('$lib/stores/persisted', () => ({
  loadSetting: vi.fn((_key: string, fallback: unknown) => fallback),
  saveSetting: vi.fn(),
}));

function room(overrides: Partial<ChatRoom> = {}): ChatRoom {
  return {
    id: 1,
    token: 't1',
    name: 'Ideas',
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    ...overrides,
  };
}

const emptyHistory = { messages: [], active_task: null, active_tasks: [] };

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

// `vi.resetModules()` gives chat a fresh copy of notices, so a statically
// imported `currentNotice` here would be a different singleton.
async function freshNotices() {
  return await import('./notices');
}

describe('chat store — promote / reconnect to Talk', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room()] });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  it('adopts the new Talk token on a fresh promote', async () => {
    api.promoteChatRoom.mockResolvedValue({
      status: 'ok',
      room: room({ talk_token: 'new-tok' }),
    });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.promoteRoom(1);
    expect(get(s.rooms)[0].talk_token).toBe('new-tok');
    expect(get(notices.currentNotice)?.severity).toBe('success');
  });

  it('adopts the replacement token when a dead binding is repaired', async () => {
    api.promoteChatRoom.mockResolvedValue({
      status: 'reconnected',
      room: room({ talk_token: 'fresh-tok' }),
    });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.promoteRoom(1);
    expect(get(s.rooms)[0].talk_token).toBe('fresh-tok');
    expect(get(notices.currentNotice)?.severity).toBe('success');
    expect(get(notices.currentNotice)?.message).toMatch(/reconnected/i);
  });

  it('says so, rather than nothing, when the room is already connected', async () => {
    api.promoteChatRoom.mockResolvedValue({
      status: 'live',
      room: room({ talk_token: 'live-tok' }),
    });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.promoteRoom(1);
    expect(get(notices.currentNotice)?.severity).toBe('warning');
    expect(get(notices.currentNotice)?.message).toMatch(/already connected/i);
  });

  it('reports an unreachable Nextcloud as an error and changes no room', async () => {
    api.promoteChatRoom.mockResolvedValue({ status: 'unreachable', room: null });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.promoteRoom(1);
    // A null room must not be merged — `{...x, ...null}` is a silent no-op, but
    // the point is that the caller can tell nothing happened.
    expect(get(s.rooms)[0].talk_token).toBeUndefined();
    expect(get(notices.currentNotice)?.severity).toBe('error');
  });

  it('tells the user the bot was removed, and adopts no new token', async () => {
    api.promoteChatRoom.mockResolvedValue({
      status: 'bot_removed',
      room: room({ talk_token: 'live-tok' }),
    });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.promoteRoom(1);
    expect(get(s.rooms)[0].talk_token).toBe('live-tok');
    expect(get(notices.currentNotice)?.severity).toBe('warning');
    expect(get(notices.currentNotice)?.message).toMatch(/removed from it/i);
  });

  it('reports a lost race and adopts the winner token when one comes back', async () => {
    api.promoteChatRoom.mockResolvedValue({
      status: 'raced',
      room: room({ talk_token: 'winner-tok' }),
    });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.promoteRoom(1);
    expect(get(s.rooms)[0].talk_token).toBe('winner-tok');
    expect(get(notices.currentNotice)?.severity).toBe('warning');
  });

  it('reports a lost race without adopting a token', async () => {
    api.promoteChatRoom.mockResolvedValue({ status: 'raced', room: null });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.promoteRoom(1);
    expect(get(s.rooms)[0].talk_token).toBeUndefined();
    expect(get(notices.currentNotice)?.severity).toBe('warning');
  });

  it('still reports a thrown request', async () => {
    api.promoteChatRoom.mockRejectedValue(new Error('API error: 500'));
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.promoteRoom(1);
    expect(get(notices.currentNotice)?.severity).toBe('error');
  });
});
