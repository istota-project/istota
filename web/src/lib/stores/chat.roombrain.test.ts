/**
 * Saving a room's brain, store side.
 *
 * Two things follow a brain change that the PATCH itself cannot do. The room's
 * model aliases were resolved through the brain it had when they were fetched
 * and the picker caches them per session, so the cache has to be dropped or the
 * modal's own "pick a new one after saving" caption walks the user back into a
 * list the next save refuses with a 400. And the response reports what the
 * change cleared, which the modal predicts before the save but cannot predict
 * for a brain somebody changed on another surface in between.
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

// Partial rather than wholesale: the store also reads `loadCommandNames` and
// `resetCommandCatalogue` from this module on its own boot and teardown paths,
// and a bare factory would leave both undefined.
const dropRoomCatalogue = vi.hoisted(() => vi.fn());
vi.mock('$lib/components/chat/autocomplete/providers', async (importOriginal) => ({
  ...((await importOriginal()) as object),
  dropRoomCatalogue,
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

describe('chat store — saving a room brain', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    dropRoomCatalogue.mockReset();
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room()] });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  it('drops the room model catalogue when the patch carried a brain', async () => {
    api.updateChatRoom.mockResolvedValue(room({ brain: 'native', model: null }));
    const s = await freshSession();
    await s.init();
    await s.updateRoomSettings(1, { brain: 'native' });
    expect(dropRoomCatalogue).toHaveBeenCalledWith(1);
    expect(get(s.rooms)[0].brain).toBe('native');
  });

  it('drops it on a clear too, which is equally a brain change', async () => {
    api.updateChatRoom.mockResolvedValue(room({ brain: null }));
    const s = await freshSession();
    await s.init();
    await s.updateRoomSettings(1, { brain: null });
    expect(dropRoomCatalogue).toHaveBeenCalledWith(1);
  });

  it('leaves it alone for a save that did not touch the brain', async () => {
    // The control: a rename or a model edit resolves against the brain the
    // cache was filled from, so throwing it away is a wasted round trip.
    api.updateChatRoom.mockResolvedValue(room({ name: 'Renamed' }));
    const s = await freshSession();
    await s.init();
    await s.updateRoomSettings(1, { name: 'Renamed' });
    expect(dropRoomCatalogue).not.toHaveBeenCalled();
  });

  it('says what the change cleared', async () => {
    api.updateChatRoom.mockResolvedValue({
      ...room({ brain: 'native', model: null, effort: null }),
      cleared: ['model', 'effort'],
    });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.updateRoomSettings(1, { brain: 'native' });
    const notice = get(notices.currentNotice);
    expect(notice?.severity).toBe('warning');
    expect(notice?.message).toMatch(/model and effort defaults were cleared/i);
  });

  it('names the model alone when the effort was not part of it', async () => {
    api.updateChatRoom.mockResolvedValue({
      ...room({ brain: 'native', model: null }),
      cleared: ['model'],
    });
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.updateRoomSettings(1, { brain: 'native' });
    expect(get(notices.currentNotice)?.message).toMatch(/model default was cleared/i);
  });

  it('says nothing when nothing was cleared', async () => {
    api.updateChatRoom.mockResolvedValue(room({ brain: 'tmux_claude' }));
    const s = await freshSession();
    await s.init();
    const notices = await freshNotices();
    notices.clearNotices();
    await s.updateRoomSettings(1, { brain: 'tmux_claude' });
    expect(get(notices.currentNotice)).toBeNull();
  });

  it('keeps the report off the room record', async () => {
    // It is a fact about the request, not about the room. Spread onto the
    // record it would sit there for the life of the session and read as a
    // standing property.
    api.updateChatRoom.mockResolvedValue({
      ...room({ brain: 'native', model: null }),
      cleared: ['model'],
    });
    const s = await freshSession();
    await s.init();
    await s.updateRoomSettings(1, { brain: 'native' });
    expect('cleared' in get(s.rooms)[0]).toBe(false);
  });
});
