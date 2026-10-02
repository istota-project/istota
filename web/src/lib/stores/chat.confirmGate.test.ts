/**
 * Approving a parked question keeps the turn above it (ISSUE-592).
 *
 * `confirm` used to empty the message before the re-run streamed, so the
 * approved answer read as a fresh reply to the original request, with the work
 * and the question that led to it gone.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { get } from 'svelte/store';
import type { ChatRoom } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

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

type Ev = { seq: number; kind: string; payload: Record<string, unknown> };

// The task's event log as the server holds it, served `seq > since` the way
// the snapshot endpoint does.
let log: Ev[] = [];

describe('chat store — approving a confirmation', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 20 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockImplementation(async (_id: number, since: number) => ({
      events: log.filter((e) => e.seq > since),
      next_seq: 0,
    }));
    api.confirmChatTask.mockResolvedValue({ ok: true });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('keeps the work and the question, and streams the re-run below them', async () => {
    const s = await freshSession();
    await s.init();
    log = [
      { seq: 1, kind: 'task_started', payload: {} },
      { seq: 2, kind: 'tool_start', payload: { tool_call_id: 'a', description: 'list' } },
      { seq: 3, kind: 'text_delta', payload: { text: 'May I delete it?' } },
      { seq: 4, kind: 'confirmation', payload: { prompt: 'May I delete it?' } },
      { seq: 5, kind: 'done', payload: {} },
    ];
    api.sendChatMessage.mockResolvedValue({ ok: true, task_id: 42 });
    await s.send('clean up');

    const turn = () => get(s.messages).find((m) => m.role === 'assistant' && m.taskId === 42)!;
    await vi.waitFor(() => expect(turn().confirmation).toBe(true));

    // What approve leaves: the question relabelled in place, `done` gone, and
    // the re-run taking the freed seq.
    log = [
      ...log.slice(0, 3),
      { seq: 4, kind: 'confirmed', payload: { prompt: 'May I delete it?' } },
      { seq: 5, kind: 'task_started', payload: {} },
      { seq: 6, kind: 'result', payload: { text: 'Deleted.' } },
      { seq: 7, kind: 'done', payload: {} },
    ];
    await s.confirm(turn().cid, 42);
    await vi.waitFor(() => expect(turn().streaming).toBe(false));

    const m = turn();
    expect(m.confirmation).toBe(false);
    expect(m.segments.map((x) => x.kind)).toEqual(['tool', 'gate', 'text']);
    expect(m.segments[1]).toMatchObject({ text: 'May I delete it?', outcome: 'approved' });
    expect(m.text).toBe('Deleted.');
    // The reopened stream asked from past the question, not from the start.
    const sinces = api.getTaskEvents.mock.calls.map((c: unknown[]) => c[1]);
    expect(sinces).toContain(4);
    s.teardown();
  });
});
