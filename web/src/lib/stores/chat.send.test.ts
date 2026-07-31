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
  getChatMessagesView: vi.fn(),
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
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.getChatMessagesView.mockResolvedValue({ messages: [], has_more: false });
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
      expect(api.sendChatMessage).toHaveBeenLastCalledWith(
        1,
        'hello',
        [att.path],
        [att.name],
        undefined,
        expect.any(String),
      );
    });

    it('re-sends the original key rather than minting a new one', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'unreachable' });
      await s.send('hello');
      const firstKey = api.sendChatMessage.mock.calls[0][5];
      expect(firstKey).toEqual(expect.any(String));

      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 55 });
      await s.retrySend(get(s.messages)[0].cid);

      // Same identity on both attempts: it is what lets the server answer a
      // retry of a send it had in fact accepted with the first turn.
      expect(api.sendChatMessage.mock.calls[1][5]).toBe(firstKey);
    });

    it('keeps the whole attachment on the payload rather than a projection', async () => {
      const s = await freshSession();
      await s.init();
      const att = attachment();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'unreachable' });
      await s.send('hello', [att]);

      // `size: 0` used to be fabricated here, putting a value through the
      // retry that was never the file's.
      expect(get(s.messages)[0].sendPayload?.attachments).toEqual([att]);
    });

    it('refuses a row whose failure a retry could not resolve', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 401, failure: 'auth' });
      await s.send('hello');
      const cid = get(s.messages)[0].cid;
      expect(get(s.messages)[0].retryable).toBe(false);

      api.sendChatMessage.mockClear();
      await s.retrySend(cid);

      // Re-POSTing a dead session fails identically, so nothing is attempted.
      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(get(s.messages)[0].sendState).toBe('failed');
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

  /**
   * `messages` is a projection of server history, and a failed send is the one
   * row with no server copy — so every rebuild of the array used to destroy the
   * only record of what the user wrote, along with the Retry that was its only
   * way back. A network outage triggers a rebuild and the failure at once.
   */
  describe('a failed send outliving a transcript rebuild', () => {
    async function failedSend() {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'unreachable' });
      await s.send('the message that never left');
      return s;
    }

    it('comes back after a round trip through another room, retry and all', async () => {
      const s = await failedSend();
      const failed = get(s.messages)[0];

      await s.selectRoom(2);
      expect(get(s.messages)).toHaveLength(0);

      await s.selectRoom(1);

      const carried = get(s.messages).filter((m) => m.sendState === 'failed');
      expect(carried).toHaveLength(1);
      expect(carried[0].cid).toBe(failed.cid);
      expect(carried[0].text).toBe('the message that never left');
      // The same row object, so what a retry needs — the host attachment paths
      // and the idempotency key — is still on it rather than reconstructed.
      expect(carried[0].sendPayload).toBe(failed.sendPayload);
      expect(carried[0].retryable).toBe(true);
    });

    it('is not carried into a room it does not belong to', async () => {
      const s = await failedSend();
      await s.selectRoom(2);
      expect(get(s.messages).some((m) => m.sendState === 'failed')).toBe(false);
    });

    it('does not duplicate a row that settled', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 7 });
      await s.send('hello');

      await s.selectRoom(2);
      await s.selectRoom(1);

      // Nothing client-only survives a settled send, so history alone rebuilds
      // the room — here, to nothing.
      expect(get(s.messages)).toHaveLength(0);
    });

    it('appears in All, which spans its room, and not in Starred, which does not', async () => {
      const s = await failedSend();

      await s.selectView('all');
      expect(get(s.messages).filter((m) => m.sendState === 'failed')).toHaveLength(1);

      await s.selectView('starred');
      expect(get(s.messages).some((m) => m.sendState === 'failed')).toBe(false);

      // Filtered out of Starred rather than discarded by it.
      await s.selectRoom(1);
      expect(get(s.messages).filter((m) => m.sendState === 'failed')).toHaveLength(1);
    });

    it('leaves with the room when it is deleted, and does not come back', async () => {
      const s = await failedSend();
      api.deleteChatRoom.mockResolvedValue({ ok: true });

      await s.deleteRoom(1);
      expect(get(s.messages).some((m) => m.sendState === 'failed')).toBe(false);

      // The assertion above holds even with the drop deleted, because the
      // delete reselects a neighbour and that rebuild replaces the array. The
      // All view is what actually reads the holding map — and `deleteRoom`
      // reselects *through* `stopActive`, whose stash would otherwise put the
      // departed room's rows straight back under the token just dropped.
      await s.selectView('all');
      expect(get(s.messages).some((m) => m.sendState === 'failed')).toBe(false);
    });
  });

  describe('the settle signal', () => {
    it('fires on the ack and not on a failure', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'unreachable' });
      await s.send('nope');
      expect(get(s.sendSettled).n).toBe(0);

      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 9 });
      await s.send('yes');
      expect(get(s.sendSettled).n).toBe(1);
    });

    it('names the room the acked send belongs to, not the one on screen', async () => {
      // Two sends can be open at once, so a bare counter let whichever acked
      // first settle the other's draft while its own send was still in flight.
      const s = await freshSession();
      await s.init();
      let release: (v: unknown) => void = () => {};
      api.sendChatMessage.mockReturnValueOnce(
        new Promise((res) => {
          release = res;
        }),
      );
      const first = s.send('from room one');

      await s.selectRoom(2);
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 2 });
      await s.send('from room two');
      expect(get(s.sendSettled).token).toBe('t2');

      release({ ok: true, status: 200, task_id: 1 });
      await first;
      // Room 1's ack lands with room 2 on screen and still names room 1.
      expect(get(s.sendSettled).token).toBe('t1');
      expect(get(s.sendSettled).n).toBe(2);
    });
  });

  describe('a command send', () => {
    it('is not offered a Retry, since a command runs inside the request', async () => {
      // `!steer` appends a note per call and `!retry` creates a task per call,
      // and the endpoint returns before it ever consults the idempotency key —
      // so a timeout cannot be re-POSTed safely.
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'timeout' });

      await s.send('!steer check the other repo too');

      const m = get(s.messages)[0];
      expect(m.sendState).toBe('failed');
      expect(m.retryable).toBe(false);
    });

    it('still offers a Retry for an ordinary message that timed out', async () => {
      const s = await freshSession();
      await s.init();
      api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'timeout' });

      await s.send('an ordinary message');

      expect(get(s.messages)[0].retryable).toBe(true);
    });
  });
});
