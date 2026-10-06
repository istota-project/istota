/**
 * #659: a followed task's turn is the room's stored row.
 *
 * The task stream used to fill the placeholder with the raw model result, so
 * an open email thread room showed the host's private note as the bot's turn
 * until a reload. The placeholder for task N is now replaced by the room's
 * assistant row for task N, which arrives over the room stream, and removed
 * when `done` arrives and the room stores no such row. These tests drive both
 * streams through the polling fallback, in both arrival orders.
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

const room: ChatRoom = {
  id: 1,
  token: 't1',
  name: 'Dinner',
  archived: false,
  created_at: '',
  updated_at: '',
  origin: 'web',
  unread_count: 0,
};

type Row = ChatHistory['messages'][number] & { room_token: string };

function row(msgId: number, over: Partial<Row>): Row {
  return {
    role: 'assistant',
    text: '',
    created_at: '2026-10-05T10:00:00Z',
    msg_id: msgId,
    starred: false,
    room_token: 't1',
    room_name: 'Dinner',
    ...over,
  } as Row;
}

const NOTE = 'Told them Thursday at 7, which clashes with your course.';
const MAIL = { to: ['alice@ext.example'], cc: [], subject: 'Re: Dinner', state: 'sent' };

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

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

/** A session in room t1 following task 77, whose user row just streamed in. */
async function following() {
  const s = await freshSession();
  await s.init();
  queueRoomEvents(
    [row(10, { role: 'user', text: 'Can we do Thursday?', task_id: 77, status: 'running' })],
    10,
  );
  await vi.advanceTimersByTimeAsync(2000);
  expect(get(s.messages).some((m) => m.role === 'assistant' && m.taskId === 77)).toBe(true);
  return s;
}

const turnsFor = (s: Awaited<ReturnType<typeof freshSession>>) =>
  get(s.messages).filter((m) => m.role === 'assistant' && m.taskId === 77);

describe('chat store — a followed turn is the stored row', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room] });
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

  it('takes the stored mail when the row lands before done', async () => {
    const s = await following();
    queueRoomEvents(
      [
        row(11, {
          text: 'Thursday at 7 works.',
          task_id: 77,
          status: 'completed',
          mail: MAIL,
        } as Partial<Row>),
      ],
      11,
    );
    await vi.advanceTimersByTimeAsync(2000);
    taskEvents([
      { kind: 'result', payload: { truncated: false } },
      { kind: 'done', payload: { stop_reason: 'completed' } },
    ]);
    await vi.advanceTimersByTimeAsync(4000);
    const turns = turnsFor(s);
    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe('Thursday at 7 works.');
    expect(turns[0].msgId).toBe(11);
    expect(turns[0].mail?.state).toBe('sent');
    expect(turns[0].streaming).toBe(false);
    s.teardown();
  });

  it('drops the placeholder at done, then shows the row when it lands', async () => {
    const s = await following();
    taskEvents([
      { kind: 'tool_start', payload: { tool_name: 'Bash', description: 'Writing the mail' } },
      { kind: 'result', payload: { truncated: false } },
      { kind: 'done', payload: { stop_reason: 'completed' } },
    ]);
    await vi.advanceTimersByTimeAsync(4000);
    expect(turnsFor(s)).toHaveLength(0);
    queueRoomEvents(
      [
        row(11, {
          text: 'Thursday at 7 works.',
          task_id: 77,
          status: 'completed',
          mail: MAIL,
        } as Partial<Row>),
      ],
      11,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const turns = turnsFor(s);
    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe('Thursday at 7 works.');
    s.teardown();
  });

  it('leaves no turn when the room stores none (NO_ACTION)', async () => {
    const s = await following();
    taskEvents([
      { kind: 'result', payload: { truncated: false } },
      { kind: 'done', payload: { stop_reason: 'completed' } },
    ]);
    await vi.advanceTimersByTimeAsync(4000);
    expect(turnsFor(s)).toHaveLength(0);
    expect(JSON.stringify(get(s.messages))).not.toContain(NOTE);
    s.teardown();
  });

  it('replaces a streamed preview with the stored body', async () => {
    const s = await following();
    taskEvents([
      { kind: 'text_delta', payload: { text: 'raw [img](/chat/files?path=gone.png)' } },
      { kind: 'result', payload: { text: 'raw (file unavailable)', truncated: false } },
      { kind: 'done', payload: { stop_reason: 'completed', msg_id: 11 } },
    ]);
    await vi.advanceTimersByTimeAsync(4000);
    queueRoomEvents([row(11, { text: 'the stored body', task_id: 77, status: 'completed' })], 11);
    await vi.advanceTimersByTimeAsync(2000);
    const turns = turnsFor(s);
    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe('the stored body');
    s.teardown();
  });

  it('keeps an ordinary answer whose done carries its row', async () => {
    const s = await following();
    taskEvents([
      { kind: 'text_delta', payload: { text: 'It is sunny.' } },
      { kind: 'result', payload: { text: 'It is sunny.', truncated: false } },
      { kind: 'done', payload: { stop_reason: 'completed', msg_id: 11 } },
    ]);
    await vi.advanceTimersByTimeAsync(4000);
    const turns = turnsFor(s);
    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe('It is sunny.');
    expect(turns[0].msgId).toBe(11);
    s.teardown();
  });

  it('keeps a parked question, which is no stored row', async () => {
    const s = await following();
    taskEvents([
      { kind: 'confirmation', payload: { prompt: 'Shall I send it?' } },
      { kind: 'done', payload: { stop_reason: 'completed' } },
    ]);
    await vi.advanceTimersByTimeAsync(4000);
    const turns = turnsFor(s);
    expect(turns).toHaveLength(1);
    expect(turns[0].confirmation).toBe(true);
    s.teardown();
  });

  it('keeps two system rows for one task apart (a private park, then its note)', async () => {
    const s = await freshSession();
    await s.init();
    queueRoomEvents(
      [
        row(20, {
          role: 'system',
          text: 'Shall I send it?',
          task_id: 77,
          confirmation: true,
        } as Partial<Row>),
      ],
      20,
    );
    await vi.advanceTimersByTimeAsync(2000);
    queueRoomEvents(
      [row(21, { role: 'system', text: 'Note: sent.', task_id: 77 } as Partial<Row>)],
      21,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const system = get(s.messages).filter((m) => m.role === 'system');
    expect(system.map((m) => [m.msgId ?? null, m.text])).toEqual([
      [20, 'Shall I send it?'],
      [21, 'Note: sent.'],
    ]);
    s.teardown();
  });

  it('keeps a failed turn, which carries its error', async () => {
    const s = await following();
    taskEvents([
      { kind: 'error', payload: { message: 'It broke.' } },
      { kind: 'done', payload: { stop_reason: 'error' } },
    ]);
    await vi.advanceTimersByTimeAsync(4000);
    const turns = turnsFor(s);
    expect(turns).toHaveLength(1);
    expect(turns[0].error).toBe(true);
    s.teardown();
  });
});
