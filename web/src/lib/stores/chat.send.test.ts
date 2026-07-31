/**
 * The send lifecycle on the user's own message (ISSUE-200).
 *
 * The optimistic transcript used to give a message exactly one appearance from
 * the moment it was typed, whether or not it ever reached the backend. Worse,
 * the one failure that *was* handled (an HTTP error response) was written into
 * the assistant placeholder, so a send failure read as "the reply failed".
 *
 * The rejection path had no handling at all: `fetch` rejects rather than
 * returning a result when the backend is unreachable, so the throw escaped an
 * un-awaited caller, `status` was never reset off 'sending', and the composer
 * stayed locked in Stop mode until reload. That is what the 'idle' assertions
 * below guard.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom, ChatAttachment } from '$lib/api';

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

const notices = vi.hoisted(() => ({
  notify: vi.fn(),
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('$lib/stores/notices', () => notices);

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

function attachment(name = 'note.txt'): ChatAttachment {
  return { path: `/host/inbox/${name}`, name, size: 12, workspace_path: `/Users/u/inbox/${name}` };
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

describe('chat store — send lifecycle', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    Object.values(notices).forEach((v) => v.mockReset());
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

  describe('a settled send', () => {
    it('leaves no send state and stamps the task id on both halves', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 42 });

      await s.send('hello');

      const msgs = get(s.messages);
      expect(msgs).toHaveLength(2);
      expect(msgs[0].role).toBe('user');
      expect(msgs[0].sendState).toBeUndefined();
      expect(msgs[0].taskId).toBe(42);
      expect(msgs[1].role).toBe('assistant');
      expect(msgs[1].taskId).toBe(42);
    });
  });

  describe('a rejected send', () => {
    it('flips the user row to failed and drops the assistant placeholder', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({
        ok: false,
        status: 500,
        failure: 'rejected',
        error: 'room is archived',
      });

      await s.send('hello');

      const msgs = get(s.messages);
      // The turn produced no assistant message, so there is no assistant row.
      expect(msgs).toHaveLength(1);
      expect(msgs[0].role).toBe('user');
      expect(msgs[0].text).toBe('hello');
      expect(msgs[0].sendState).toBe('failed');
      expect(msgs[0].sendError).toContain('room is archived');
      expect(msgs[0].retryable).toBe(true);
      expect(get(s.status)).toBe('idle');
    });

    it('carries the rate-limit wait into the failure message', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({
        ok: false,
        status: 429,
        failure: 'rate_limit',
        retry_after: 90,
      });

      await s.send('hello');

      expect(get(s.messages)[0].sendError).toContain('90');
    });

    it('names the server unreachable rather than blaming the reply', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'unreachable' });

      await s.send('hello');

      const m = get(s.messages)[0];
      expect(m.sendState).toBe('failed');
      expect(m.sendError).toMatch(/unreachable/i);
      expect(m.retryable).toBe(true);
    });

    it('offers no retry for a verdict on the request itself', async () => {
      const s = await freshSession();
      await s.init();
      // 409 "room is archived": re-POSTing the same payload fails identically,
      // so a Retry button would be the same lie the auth case avoids.
      api.sendChatMessage.mockResolvedValue({
        ok: false,
        status: 409,
        failure: 'rejected',
        error: 'room is archived',
      });

      await s.send('hello');

      expect(get(s.messages)[0].retryable).toBe(false);
    });

    it('keeps retry for a 4xx that means later rather than no', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 408, failure: 'rejected' });

      await s.send('hello');

      expect(get(s.messages)[0].retryable).toBe(true);
    });

    it('names a timeout as an unanswered server, not an unreachable one', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'timeout' });

      await s.send('hello');

      expect(get(s.messages)[0].sendError).toMatch(/didn’t respond/);
      expect(get(s.messages)[0].retryable).toBe(true);
    });

    it('offers no retry for an expired session', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 401, failure: 'auth' });

      await s.send('hello');

      const m = get(s.messages)[0];
      expect(m.sendState).toBe('failed');
      expect(m.sendError).toMatch(/session/i);
      // Retrying cannot succeed, so the affordance would lie.
      expect(m.retryable).toBe(false);
    });

    it('releases the composer when the send transport throws outright', async () => {
      const s = await freshSession();
      await s.init();
      // The store must survive a throw even though `sendChatMessage` no longer
      // rejects: an un-reset 'sending' is what locked the composer until reload.
      api.sendChatMessage.mockRejectedValue(new TypeError('Failed to fetch'));

      await expect(s.send('hello')).resolves.toBeUndefined();

      expect(get(s.status)).toBe('idle');
      expect(get(s.messages)[0].sendState).toBe('failed');
    });

    it('does not blame the send for a failure after the backend acked', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 12 });
      // Thrown downstream of the ack: the task really is running, so this must
      // not be reported as a message that never left — and the placeholder it
      // would delete belongs to a turn that is genuinely under way.
      api.chatStreamUrl.mockImplementation(() => {
        throw new Error('boom');
      });

      await expect(s.send('hello')).resolves.toBeUndefined();

      const msgs = get(s.messages);
      expect(msgs[0].sendState).toBeUndefined();
      expect(msgs[0].taskId).toBe(12);
      expect(msgs.some((m) => m.role === 'assistant')).toBe(true);
      // Past the ack the turn owns its own status: forcing 'idle' here would
      // tell the composer the room is free while a stream is live.
      expect(get(s.status)).toBe('streaming');
    });
  });

  describe('the pending indicator', () => {
    it('stays hidden while the send is merely fast, and opens once it is slow', async () => {
      vi.useFakeTimers();
      const s = await freshSession();
      await s.init();
      let settle: (v: unknown) => void = () => {};
      api.sendChatMessage.mockReturnValue(
        new Promise((r) => {
          settle = r;
        }),
      );

      const sending = s.send('hello');
      await Promise.resolve();
      // Truthful state from the moment the row exists...
      expect(get(s.messages)[0].sendState).toBe('sending');
      // ...but nothing on screen yet: a mark that flashes for one frame is
      // noise, and the common send resolves well inside the grace.
      expect(get(s.messages)[0].showSending).toBeFalsy();

      await vi.advanceTimersByTimeAsync(500);
      expect(get(s.messages)[0].showSending).toBe(true);

      settle({ ok: true, status: 200, task_id: 7 });
      await sending;
      expect(get(s.messages)[0].showSending).toBeFalsy();
      expect(get(s.messages)[0].sendState).toBeUndefined();
    });
  });

  describe('retry', () => {
    it('replays the original attachments in place rather than appending a row', async () => {
      const s = await freshSession();
      await s.init();
      const att = attachment();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'unreachable' });
      await s.send('hello', [att]);

      const failed = get(s.messages)[0];
      expect(get(s.messages)).toHaveLength(1);

      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 55 });
      await s.retrySend(failed.cid);

      const msgs = get(s.messages);
      // Same row, not a second user bubble.
      expect(msgs.filter((m) => m.role === 'user')).toHaveLength(1);
      expect(msgs[0].cid).toBe(failed.cid);
      expect(msgs[0].sendState).toBeUndefined();
      expect(msgs[0].taskId).toBe(55);
      // The host path the POST takes, which the rendered row does not hold.
      expect(api.sendChatMessage).toHaveBeenLastCalledWith(1, 'hello', [att.path], [att.name]);
    });

    it('restores the assistant placeholder the failure removed', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'unreachable' });
      await s.send('hello');

      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 8 });
      await s.retrySend(get(s.messages)[0].cid);

      const msgs = get(s.messages);
      expect(msgs).toHaveLength(2);
      expect(msgs[1].role).toBe('assistant');
      expect(msgs[1].taskId).toBe(8);
    });

    it('is refused while another turn is in flight', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'unreachable' });
      await s.send('first');
      const failedCid = get(s.messages)[0].cid;

      // A second send, still open. `runTurn` is not re-entrant: its entry drains
      // the single `pendingSend` slot, so retrying here would release this
      // send's echo before its task id was stamped — two bubbles for one
      // message, and two streams bound to one task.
      let settle: (v: unknown) => void = () => {};
      api.sendChatMessage.mockReturnValue(
        new Promise((r) => {
          settle = r;
        }),
      );
      const second = s.send('second');
      await Promise.resolve();
      api.sendChatMessage.mockClear();

      await s.retrySend(failedCid);
      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(get(s.messages).find((m) => m.cid === failedCid)?.sendState).toBe('failed');

      settle({ ok: true, status: 200, task_id: 21 });
      await second;
    });

    it('is a no-op on a row that did not fail', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 3 });
      await s.send('hello');
      api.sendChatMessage.mockClear();

      await s.retrySend(get(s.messages)[0].cid);

      expect(api.sendChatMessage).not.toHaveBeenCalled();
    });
  });
});
