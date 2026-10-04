/**
 * Retrying a failed turn's task from the transcript (ISSUE-631).
 *
 * The click POSTs to `/chat/tasks/{id}/retry` and nothing else: the retry's
 * user turn and its stream arrive over the room stream, the way a typed
 * `!retry` does. The failed turn stays, marked with what replaced it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
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

const notices = vi.hoisted(() => ({
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('./notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./notices')>()),
  ...notices,
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

const history = {
  messages: [
    { role: 'user', text: 'do the thing', task_id: 41, created_at: '2026-10-04T12:00:00Z' },
    {
      role: 'assistant',
      text: 'Task failed after 3 attempts',
      task_id: 41,
      status: 'failed',
      created_at: '2026-10-04T12:00:01Z',
    },
  ],
  active_task: null,
  active_tasks: [],
};

describe('chat store — retryTask', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    Object.values(notices).forEach((f) => f.mockReset());
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(history);
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [], next_seq: 0 });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  async function failedTurnCid(s: Awaited<ReturnType<typeof freshSession>>) {
    await s.init();
    const failed = get(s.messages).find((m) => m.role === 'assistant' && m.taskId === 41);
    expect(failed).toBeDefined();
    return failed!.cid;
  }

  it('POSTs the retry and keeps the failed turn, marked with the new task', async () => {
    const s = await freshSession();
    const cid = await failedTurnCid(s);
    api.retryChatTask.mockResolvedValue({
      status: 'queued',
      task_id: 52,
      retried_task_id: 41,
      run_now: false,
      message: 'Retrying task #41 as #52: do the thing',
    });

    await s.retryTask(cid, 'retry');

    expect(api.retryChatTask).toHaveBeenCalledWith(41, 'retry');
    const after = get(s.messages).find((m) => m.cid === cid);
    expect(after?.retriedAs).toBe(52);
    expect(after?.text).toBe('Task failed after 3 attempts');
    // A room turn reports itself by streaming in; no notice for it.
    expect(notices.notifySuccess).not.toHaveBeenCalled();
  });

  it('passes Continue through as resume', async () => {
    const s = await freshSession();
    const cid = await failedTurnCid(s);
    api.retryChatTask.mockResolvedValue({
      status: 'queued',
      task_id: 53,
      retried_task_id: 41,
      run_now: false,
      message: '',
    });
    await s.retryTask(cid, 'resume');
    expect(api.retryChatTask).toHaveBeenCalledWith(41, 'resume');
  });

  it('reports a refusal and leaves the turn retryable', async () => {
    const s = await freshSession();
    const cid = await failedTurnCid(s);
    api.retryChatTask.mockRejectedValue(new Error('Task #41 belongs to another user.'));

    await s.retryTask(cid, 'retry');

    expect(notices.notifyError).toHaveBeenCalledWith(
      'Task #41 belongs to another user.',
      expect.anything(),
    );
    expect(get(s.messages).find((m) => m.cid === cid)?.retriedAs).toBeUndefined();
  });

  it('says so when a scheduled job runs somewhere else', async () => {
    const s = await freshSession();
    const cid = await failedTurnCid(s);
    api.retryChatTask.mockResolvedValue({
      status: 'queued',
      task_id: 60,
      retried_task_id: 41,
      run_now: true,
      message: "Running job 'digest' now as #60 (re-run of #41).",
    });
    await s.retryTask(cid, 'retry');
    expect(notices.notifySuccess).toHaveBeenCalledWith(
      "Running job 'digest' now as #60 (re-run of #41).",
      expect.anything(),
    );
  });

  it('does nothing for a turn that is not a failure, or was already retried', async () => {
    const s = await freshSession();
    const cid = await failedTurnCid(s);
    const user = get(s.messages).find((m) => m.role === 'user')!;
    await s.retryTask(user.cid, 'retry');
    api.retryChatTask.mockResolvedValue({
      status: 'queued',
      task_id: 52,
      retried_task_id: 41,
      run_now: false,
      message: '',
    });
    await s.retryTask(cid, 'retry');
    await s.retryTask(cid, 'retry');
    expect(api.retryChatTask).toHaveBeenCalledTimes(1);
  });
});
