/**
 * #665: an email thread room shows no parked question.
 *
 * The thread room is the record of the mail thread. A question its task parks
 * on is the host's private room's (and the bell's), so the open thread room
 * must not grow a confirmation card from the task stream, whether it was
 * following the task when it parked or picked it up already parked.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
  chatRoomStreamUrl: vi.fn(() => '/stream'),
  chatStreamUrl: vi.fn(() => '/task-stream'),
});

vi.mock('$lib/stores/persisted', () => ({
  loadSetting: vi.fn(() => null),
  saveSetting: vi.fn(),
}));

function roomOf(emailThread: boolean): ChatRoom {
  return {
    id: 1,
    token: 't1',
    name: 'Book club',
    archived: false,
    created_at: '',
    updated_at: '',
    origin: emailThread ? 'email' : 'web',
    unread_count: 0,
    read_only: emailThread,
    email_thread: emailThread,
  };
}

type Row = ChatHistory['messages'][number] & { room_token: string };

function userRow(status: string): Row {
  return {
    role: 'user',
    text: 'Come on Saturday?',
    created_at: '2026-10-05T10:00:00Z',
    msg_id: 10,
    starred: false,
    room_token: 't1',
    room_name: 'Book club',
    task_id: 77,
    status,
  } as Row;
}

function queueRoomEvents(events: Row[], cursor: number) {
  api.getRoomEvents.mockResolvedValueOnce({ events, cursor, gap: false });
  api.getRoomEvents.mockResolvedValue({ events: [], cursor, gap: false });
}

function taskEvents(events: { kind: string; payload: Record<string, unknown> }[]) {
  const seqd = events.map((e, i) => ({ seq: i + 1, ...e }));
  api.getTaskEvents.mockImplementation(async (_id: number, since: number) => ({
    events: seqd.filter((e) => e.seq > since),
  }));
}

async function session(emailThread: boolean) {
  api.getChatRooms.mockResolvedValue({ rooms: [roomOf(emailThread)] });
  vi.resetModules();
  const mod = await import('./chat');
  const s = mod.getChatSession();
  await s.init();
  return s;
}

const turnsFor = (s: Awaited<ReturnType<typeof session>>) =>
  get(s.messages).filter((m) => m.role === 'assistant' && m.taskId === 77);

const PARKED = [
  { kind: 'task_started', payload: {} },
  { kind: 'confirmation', payload: { prompt: 'Send the reply?' } },
];

describe('chat store — a thread room shows no parked question', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatRoomStreamUrl.mockReturnValue('/stream');
    api.chatStreamUrl.mockReturnValue('/task-stream');
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.getTaskEvents.mockResolvedValue({ events: [] });
    (globalThis as any).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('drops the turn when a followed thread task parks', async () => {
    const s = await session(true);
    queueRoomEvents([userRow('running')], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(turnsFor(s)).toHaveLength(1);
    taskEvents(PARKED);
    await vi.advanceTimersByTimeAsync(4000);
    expect(turnsFor(s)).toEqual([]);
    expect(get(s.messages).some((m) => m.confirmation)).toBe(false);
    expect(get(s.status)).toBe('idle');
    s.teardown();
  });

  it('opens no turn for a thread task that is already parked', async () => {
    const s = await session(true);
    taskEvents(PARKED);
    queueRoomEvents([userRow('pending_confirmation')], 10);
    await vi.advanceTimersByTimeAsync(4000);
    expect(turnsFor(s)).toEqual([]);
    expect(get(s.messages).some((m) => m.confirmation)).toBe(false);
    s.teardown();
  });

  it('still shows the card in an ordinary room', async () => {
    const s = await session(false);
    taskEvents(PARKED);
    queueRoomEvents([userRow('pending_confirmation')], 10);
    await vi.advanceTimersByTimeAsync(4000);
    const turns = turnsFor(s);
    expect(turns).toHaveLength(1);
    expect(turns[0].confirmation).toBe(true);
    s.teardown();
  });
});
