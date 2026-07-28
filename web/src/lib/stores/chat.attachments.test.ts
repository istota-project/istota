/**
 * Attachment chips must survive a transcript rebuild.
 *
 * The live chip comes from the composer's in-memory file list, which is gone
 * the moment the store reloads a room — so leaving a room and coming back used
 * to drop it. The server now persists display names on the user turn; these
 * cover the client half: history rows carry them onto the rendered message, and
 * the streamed echo of a turn we sent doesn't wipe the optimistic ones.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
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

function history(messages: ChatHistory['messages']) {
  return {
    messages,
    active_task: null,
    active_tasks: [],
    has_more: false,
    oldest_cursor: null,
  };
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — attachment chips', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  it('carries a history turn’s attachment names onto the message', async () => {
    api.getRoomMessages.mockResolvedValue(
      history([
        {
          role: 'user',
          text: 'summarize this',
          task_id: 7,
          created_at: '2026-06-10T12:00:00Z',
          attachments: ['note.txt', 'chart.png'],
        },
      ]),
    );
    const s = await freshSession();
    await s.init();
    const user = get(s.messages).find((m) => m.role === 'user');
    expect(user?.attachments).toEqual(['note.txt', 'chart.png']);
  });

  it('leaves attachments undefined for a plain turn', async () => {
    api.getRoomMessages.mockResolvedValue(
      history([{ role: 'user', text: 'no files', task_id: 8, created_at: '2026-06-10T12:00:00Z' }]),
    );
    const s = await freshSession();
    await s.init();
    expect(get(s.messages).find((m) => m.role === 'user')?.attachments).toBeUndefined();
  });

  it('keeps the chip when the room is left and re-entered', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    const withFile = history([
      {
        role: 'user',
        text: 'look at this',
        task_id: 9,
        created_at: '2026-06-10T12:00:00Z',
        attachments: ['receipt.pdf'],
      },
    ]);
    api.getRoomMessages.mockImplementation(async (id: number) =>
      id === 1 ? withFile : history([]),
    );
    const s = await freshSession();
    await s.init();
    await s.selectRoom(2);
    await s.selectRoom(1);
    expect(get(s.messages).find((m) => m.role === 'user')?.attachments).toEqual(['receipt.pdf']);
  });

  it('sends the display names alongside the paths', async () => {
    api.getRoomMessages.mockResolvedValue(history([]));
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 11 });
    api.getTaskEvents.mockResolvedValue({ events: [] });
    const s = await freshSession();
    await s.init();
    await s.send('read this', [{ path: '/inbox/note-a1b2c3d4.txt', name: 'note.txt', size: 11 }]);
    expect(api.sendChatMessage).toHaveBeenCalledWith(
      1,
      'read this',
      ['/inbox/note-a1b2c3d4.txt'],
      ['note.txt'],
    );
  });

  it('carries a history turn’s link paths onto the message', async () => {
    api.getRoomMessages.mockResolvedValue(
      history([
        {
          role: 'user',
          text: 'read this',
          task_id: 12,
          created_at: '2026-06-10T12:00:00Z',
          attachments: ['note.txt', 'shared.png'],
          attachment_paths: ['/Users/alice/inbox/web-chat/note-a1.txt', null],
        },
      ]),
    );
    const s = await freshSession();
    await s.init();
    const user = get(s.messages).find((m) => m.role === 'user');
    // Positional, nulls preserved: an unlinkable file holds its slot rather
    // than shifting a link onto the wrong chip.
    expect(user?.attachmentPaths).toEqual(['/Users/alice/inbox/web-chat/note-a1.txt', null]);
  });

  it('leaves link paths undefined for a turn the server can’t serve', async () => {
    api.getRoomMessages.mockResolvedValue(
      history([
        {
          role: 'user',
          text: 'look',
          task_id: 13,
          created_at: '2026-06-10T12:00:00Z',
          attachments: ['stray.png'],
        },
      ]),
    );
    const s = await freshSession();
    await s.init();
    expect(get(s.messages).find((m) => m.role === 'user')?.attachmentPaths).toBeUndefined();
  });

  it('links the optimistic chip from the upload’s own answer', async () => {
    // Otherwise a just-sent chip stays inert for the whole session: the
    // optimistic row is deduped against the streamed echo, not replaced by it.
    api.getRoomMessages.mockResolvedValue(history([]));
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 14 });
    api.getTaskEvents.mockResolvedValue({ events: [] });
    const s = await freshSession();
    await s.init();
    await s.send('read this', [
      {
        path: '/mnt/nc/Users/alice/inbox/web-chat/note-a1.txt',
        name: 'note.txt',
        size: 11,
        workspace_path: '/Users/alice/inbox/web-chat/note-a1.txt',
      },
    ]);
    const user = get(s.messages).find((m) => m.role === 'user');
    expect(user?.attachmentPaths).toEqual(['/Users/alice/inbox/web-chat/note-a1.txt']);
  });

  it('leaves the optimistic chip inert when the upload gave no path', async () => {
    api.getRoomMessages.mockResolvedValue(history([]));
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 15 });
    api.getTaskEvents.mockResolvedValue({ events: [] });
    const s = await freshSession();
    await s.init();
    await s.send('read this', [{ path: '/tmp/stray.png', name: 'stray.png', size: 1 }]);
    const user = get(s.messages).find((m) => m.role === 'user');
    expect(user?.attachmentPaths).toEqual([null]);
  });
});
