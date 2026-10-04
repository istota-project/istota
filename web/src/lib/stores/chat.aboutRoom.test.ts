/**
 * A send linked to an email thread with no row to reply to (hidden email
 * threads, section 0c): the composer's link target goes out as `aboutRoom`
 * on the POST, and it has to survive every path a send takes before it lands
 * (a retry, the busy queue, a page reload of that queue), or the message is
 * quietly sent unlinked.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { get } from 'svelte/store';
import type { ChatRoom } from '$lib/api';
import { SEND_QUEUE_STORAGE_KEY } from './sendQueue';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

const persisted = vi.hoisted(() => {
  const store = new Map<string, string>();
  return {
    store,
    loadSetting: vi.fn((key: string, fallback: unknown) =>
      store.has(key) ? JSON.parse(store.get(key) as string) : fallback,
    ),
    saveSetting: vi.fn((key: string, value: unknown) => {
      store.set(key, JSON.stringify(value));
    }),
  };
});
vi.mock('$lib/stores/persisted', () => persisted);

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

const optsOf = (call: number) => api.sendChatMessage.mock.calls[call].at(-1);

function storedQueue(token: string, user = 'alice'): Record<string, unknown>[] {
  const raw = persisted.store.get(SEND_QUEUE_STORAGE_KEY);
  return raw ? (JSON.parse(raw)[`${user}:room:${token}`] ?? []) : [];
}

describe('chat store — a send linked to a thread', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    persisted.store.clear();
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500, user_id: 'alice' });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatStreamUrl.mockReturnValue('/stream');
    api.getTaskEvents.mockResolvedValue({ events: [], next_seq: 0 });
    api.listOutboundDrafts.mockResolvedValue({ drafts: [] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('posts the thread as aboutRoom', async () => {
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 7 });
    const s = await freshSession();
    await s.init();

    await s.send('tell them I am in', [], undefined, 'thr-1');

    expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
    expect(optsOf(0)).toMatchObject({ aboutRoom: 'thr-1' });
  });

  it('a retry posts it again', async () => {
    api.sendChatMessage.mockResolvedValueOnce({
      ok: false,
      status: 500,
      failure: 'rejected',
      error: 'boom',
    });
    const s = await freshSession();
    await s.init();
    await s.send('tell them I am in', [], undefined, 'thr-1');
    const failed = get(s.messages).find((m) => m.sendState === 'failed')!;

    api.sendChatMessage.mockResolvedValueOnce({ ok: true, status: 200, task_id: 9 });
    await s.retrySend(failed.cid);

    expect(optsOf(1)).toMatchObject({ aboutRoom: 'thr-1' });
  });

  it('a message queued behind a running turn keeps it, in memory and in storage', async () => {
    vi.useFakeTimers();
    const s = await freshSession();
    await s.init();
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 42 });
    await s.send('the first turn');
    await vi.advanceTimersByTimeAsync(0);
    api.sendChatMessage.mockClear();

    await s.send('and this one', [], undefined, 'thr-1');
    expect(api.sendChatMessage).not.toHaveBeenCalled();
    expect(storedQueue('t1')[0]).toMatchObject({ aboutRoom: 'thr-1' });

    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });
    api.getTaskEvents.mockResolvedValueOnce({
      events: [{ seq: 1, kind: 'done', payload: {} }],
      next_seq: 1,
    });
    await vi.advanceTimersByTimeAsync(2000);

    expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
    expect(api.sendChatMessage.mock.calls[0][1]).toBe('and this one');
    expect(optsOf(0)).toMatchObject({ aboutRoom: 'thr-1' });
  });

  it('a queue restored from storage drains with it', async () => {
    persisted.store.set(
      SEND_QUEUE_STORAGE_KEY,
      JSON.stringify({
        'alice:room:t1': [
          {
            cid: 1,
            text: 'from before the reload',
            attachments: [],
            held: false,
            queuedAt: Date.now(),
            reason: 'offline',
            aboutRoom: 'thr-1',
          },
        ],
      }),
    );
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 5 });
    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalled());
    expect(optsOf(0)).toMatchObject({ aboutRoom: 'thr-1' });
  });
});
