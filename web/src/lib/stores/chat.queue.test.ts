/**
 * The send queue: a message typed while a turn is running (ISSUE-238).
 *
 * Before this, `send()` returned silently when the room was busy — the
 * composer's mode gate meant nothing could reach it, and the store's own guard
 * was the backstop. The message is queued now and drains into the same
 * non-re-entrant `runTurn` when the running turn settles.
 *
 * Every test here asserts on the *row* as well as on the POST count, and that
 * is deliberate. "No POST went out" was true of the old code too — a mid-turn
 * send was a silent no-op — so a queue test resting on the call count alone
 * would pass against the behaviour it was written to replace. The
 * `sendState: 'queued'` row in the transcript is the thing only the new path
 * produces.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom, ChatAttachment } from '$lib/api';
import type { ChatMessage } from './segments';

const api = vi.hoisted(() => ({
  getChatConfig: vi.fn(),
  getChatRooms: vi.fn(),
  getRoomMessages: vi.fn(),
  getChatMessagesView: vi.fn(),
  getRoomEvents: vi.fn(),
  markRoomRead: vi.fn(),
  markAllRoomsRead: vi.fn(),
  getTaskEvents: vi.fn(),
  sendChatMessage: vi.fn(),
  createChatRoom: vi.fn(),
  updateChatRoom: vi.fn(),
  deleteChatRoom: vi.fn(),
  promoteChatRoom: vi.fn(),
  cancelChatTask: vi.fn(),
  confirmChatTask: vi.fn(),
  chatStreamUrl: vi.fn(),
  chatRoomStreamUrl: vi.fn(),
  listOutboundDrafts: vi.fn(),
  fetchChatCommands: vi.fn(),
  ChatRoomBusyError: class extends Error {},
  ChatMessageBusyError: class extends Error {},
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

const CATALOGUE = {
  commands: [
    { name: 'steer', help: 'Send a note into the running task' },
    { name: 'status', help: 'What is running' },
  ],
  model_aliases: [],
};

/**
 * A store whose module graph is fresh, with the command catalogue primed
 * through the same graph — `resetModules` drops the providers' per-session
 * cache along with everything else, and an empty catalogue refuses every
 * command, which is the wrong premise for the inline-command test.
 */
async function freshSession() {
  vi.resetModules();
  api.fetchChatCommands.mockResolvedValue(CATALOGUE);
  const providers = await import('$lib/components/chat/autocomplete/providers');
  await providers.loadCommandNames();
  const mod = await import('./chat');
  return mod.getChatSession();
}

/** Flush the microtask queue (and any zero-delay timer) under fake timers. */
async function flush() {
  await vi.advanceTimersByTimeAsync(0);
}

/**
 * Deliver one batch of task events to whatever stream is currently polling.
 *
 * `mockResolvedValueOnce` rather than `mockResolvedValue`, because a terminal
 * left standing would also be handed to the *next* task's stream — which opens
 * with an immediate poll inside this same window — and settle a turn that has
 * only just started.
 */
async function emit(...kinds: string[]) {
  api.getTaskEvents.mockResolvedValueOnce({
    events: kinds.map((kind, i) => ({ seq: i + 1, kind, payload: {} })),
    next_seq: kinds.length,
  });
  await vi.advanceTimersByTimeAsync(2000);
}

/** A session with one turn streaming in room 1 under `taskId`. */
async function streaming(taskId = 42) {
  const s = await freshSession();
  await s.init();
  api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: taskId });
  await s.send('the first turn');
  expect(get(s.status)).toBe('streaming');
  await flush();
  api.sendChatMessage.mockClear();
  return s;
}

const queuedRows = (msgs: ChatMessage[]) => msgs.filter((m) => m.sendState === 'queued');

describe('chat store — the send queue', () => {
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
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [], next_seq: 0 });
    api.cancelChatTask.mockResolvedValue({ ok: true });
    api.listOutboundDrafts.mockResolvedValue({ drafts: [] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe('entering the queue', () => {
    it('queues a message typed into a busy room instead of dropping it', async () => {
      const s = await streaming();

      await s.send('and another thing');

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      const queued = queuedRows(get(s.messages));
      expect(queued).toHaveLength(1);
      expect(queued[0].text).toBe('and another thing');
      expect(queued[0].role).toBe('user');
      expect(queued[0].roomToken).toBe('t1');
      // The queue holds what will be sent, so the row carries no payload of
      // its own until it drains.
      expect(queued[0].sendPayload).toBeUndefined();
      expect(queued[0].queueHeld).toBeUndefined();
      // Nothing that belongs to the running turn was touched.
      expect(get(s.status)).toBe('streaming');
      expect(get(s.activeTaskId)).toBe(42);
    });

    it('keeps two queued messages in the order they were typed', async () => {
      const s = await streaming();

      await s.send('first');
      await s.send('second');

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRows(get(s.messages)).map((m) => m.text)).toEqual(['first', 'second']);
    });

    it('carries the attachments and the optimistic quote onto the queued row', async () => {
      const s = await streaming();
      const file = attachment('spec.pdf');

      await s.send('have a look', [file], { msgId: 7, role: 'assistant', excerpt: 'earlier' });

      const [queued] = queuedRows(get(s.messages));
      expect(queued.attachments).toEqual(['spec.pdf']);
      expect(queued.attachmentPaths).toEqual(['/Users/u/inbox/spec.pdf']);
      expect(queued.replyTo?.msgId).toBe(7);
    });
  });

  describe('draining', () => {
    it('sends exactly one queued message when the turn ends done', async () => {
      const s = await streaming();
      await s.send('first');
      await s.send('second');
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });

      await emit('done');

      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('first');
      // The second is still waiting, and still unheld — the turn finished
      // normally, so nothing about it says "stop".
      const queued = queuedRows(get(s.messages));
      expect(queued.map((m) => m.text)).toEqual(['second']);
      expect(queued[0].queueHeld).toBeUndefined();
    });

    it('sends the second when the first drained turn settles', async () => {
      const s = await streaming();
      await s.send('first');
      await s.send('second');
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });
      await emit('done');
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 44 });

      await emit('done');

      expect(api.sendChatMessage).toHaveBeenCalledTimes(2);
      expect(api.sendChatMessage.mock.calls[1][1]).toBe('second');
      expect(queuedRows(get(s.messages))).toHaveLength(0);
    });

    it('drains into the row that is already on screen rather than a second bubble', async () => {
      const s = await streaming();
      await s.send('and another thing');
      const queuedCid = queuedRows(get(s.messages))[0].cid;
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });

      await emit('done');

      const mine = get(s.messages).filter(
        (m) => m.role === 'user' && m.text === 'and another thing',
      );
      expect(mine).toHaveLength(1);
      expect(mine[0].cid).toBe(queuedCid);
      expect(mine[0].taskId).toBe(43);
      expect(mine[0].sendState).toBeUndefined();
    });

    it('stamps the drained row with a payload so a later failure has its Retry', async () => {
      const s = await streaming();
      const file = attachment('spec.pdf');
      await s.send('have a look', [file]);
      // Slow POST, so the row is observable mid-flight.
      let release: (v: unknown) => void = () => {};
      api.sendChatMessage.mockReturnValue(
        new Promise((r) => {
          release = r;
        }),
      );

      await emit('done');

      const [row] = get(s.messages).filter((m) => m.text === 'have a look');
      expect(row.sendState).toBe('sending');
      expect(row.sendPayload?.text).toBe('have a look');
      expect(row.sendPayload?.attachments).toEqual([file]);
      release({ ok: true, status: 200, task_id: 43 });
      await flush();
    });
  });

  describe('holding', () => {
    it('holds the queue when the turn is cancelled', async () => {
      const s = await streaming();
      await s.send('first');
      await s.send('second');

      await emit('cancelled');

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      const queued = queuedRows(get(s.messages));
      expect(queued).toHaveLength(2);
      expect(queued.every((m) => m.queueHeld === true)).toBe(true);
    });

    it('holds the queue when the turn errors', async () => {
      const s = await streaming();
      await s.send('and another thing');

      await emit('error');

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRows(get(s.messages))[0].queueHeld).toBe(true);
    });

    it('holds the queue when the turn parks on a confirmation', async () => {
      const s = await streaming();
      await s.send('and another thing');

      // The pause sets `status` idle on its own, so without the hold the queue
      // would fire past an unanswered question.
      await emit('confirmation', 'done');

      expect(get(s.status)).toBe('idle');
      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRows(get(s.messages))[0].queueHeld).toBe(true);
    });

    it('holds the rest of the queue when a drained send fails', async () => {
      const s = await streaming();
      await s.send('first');
      await s.send('second');
      api.sendChatMessage.mockResolvedValue({
        ok: false,
        status: 500,
        failure: 'rejected',
        error: 'boom',
      });

      await emit('done');

      const msgs = get(s.messages);
      const failed = msgs.find((m) => m.text === 'first');
      expect(failed?.sendState).toBe('failed');
      expect(failed?.retryable).toBe(true);
      const queued = queuedRows(msgs);
      expect(queued.map((m) => m.text)).toEqual(['second']);
      expect(queued[0].queueHeld).toBe(true);
    });

    it('does not drain a held queue when a later turn ends done', async () => {
      const s = await streaming();
      await s.send('and another thing');
      await emit('cancelled');
      expect(queuedRows(get(s.messages))[0].queueHeld).toBe(true);

      // A second turn in the same room, started and finished normally: the
      // hold is on the entry, not on the turn, so it survives.
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 50 });
      await s.send('a fresh message');
      await flush();
      api.sendChatMessage.mockClear();
      await emit('done');

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRows(get(s.messages))[0].text).toBe('and another thing');
    });
  });

  describe('the three verbs', () => {
    it('sends a held entry on release when the room is idle', async () => {
      const s = await streaming();
      await s.send('and another thing');
      await emit('cancelled');
      const cid = queuedRows(get(s.messages))[0].cid;
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });

      await s.releaseQueued(cid);

      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('and another thing');
      expect(queuedRows(get(s.messages))).toHaveLength(0);
    });

    it('does not send a released entry while the room is busy', async () => {
      const s = await streaming();
      await s.send('and another thing');
      await emit('error');
      const cid = queuedRows(get(s.messages))[0].cid;
      // A new turn takes the room back over before the release.
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 51 });
      await s.send('a fresh message');
      await flush();
      api.sendChatMessage.mockClear();

      await s.releaseQueued(cid);

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      const [queued] = queuedRows(get(s.messages));
      expect(queued.text).toBe('and another thing');
      // Released, so the next settle takes it.
      expect(queued.queueHeld).toBeUndefined();
    });

    it('removes a queued message from the row list and from the queue', async () => {
      const s = await streaming();
      await s.send('first');
      await s.send('second');
      const cid = queuedRows(get(s.messages))[0].cid;

      s.removeQueued(cid);

      expect(queuedRows(get(s.messages)).map((m) => m.text)).toEqual(['second']);
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });
      await emit('done');
      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('second');
    });

    it('hands an edited message back to the composer with its room and files', async () => {
      const s = await streaming();
      const file = attachment('spec.pdf');
      await s.send('have a look', [file]);
      const cid = queuedRows(get(s.messages))[0].cid;
      const before = get(s.sendReturned).n;

      s.editQueued(cid);

      const returned = get(s.sendReturned);
      expect(returned.n).toBe(before + 1);
      expect(returned.text).toBe('have a look');
      expect(returned.attachments).toEqual([file]);
      expect(returned.token).toBe('t1');
      expect(queuedRows(get(s.messages))).toHaveLength(0);
      // And it is out of the queue, not merely off the transcript.
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });
      await emit('done');
      expect(api.sendChatMessage).not.toHaveBeenCalled();
    });

    it('ignores a cid that names no queued message', async () => {
      const s = await streaming();
      await s.send('and another thing');
      const before = get(s.messages).length;

      s.removeQueued(99999);
      s.editQueued(99999);
      await s.releaseQueued(99999);

      expect(get(s.messages)).toHaveLength(before);
      expect(get(s.sendReturned).n).toBe(0);
    });
  });

  describe('room switches', () => {
    it('keeps a queued entry across a room switch and puts its row back', async () => {
      const s = await streaming();
      await s.send('and another thing');
      // Room 1 is still working when we come back, so nothing drains and the
      // row is observable in its queued state.
      api.getRoomMessages.mockImplementation(async (roomId: number) =>
        roomId === 1
          ? {
              messages: [],
              active_task: { id: 42, status: 'running' },
              active_tasks: [{ id: 42, status: 'running' }],
            }
          : { messages: [], active_task: null, active_tasks: [] },
      );

      await s.selectRoom(2);
      expect(queuedRows(get(s.messages))).toHaveLength(0);
      await s.selectRoom(1);
      await flush();

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      const queued = queuedRows(get(s.messages));
      expect(queued).toHaveLength(1);
      expect(queued[0].text).toBe('and another thing');
    });

    it('drains on returning to a room whose turn finished while away', async () => {
      const s = await streaming();
      await s.send('and another thing');
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });

      await s.selectRoom(2);
      await s.selectRoom(1);
      await flush();

      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][0]).toBe(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('and another thing');
    });

    it('never drains a room that is not the active one', async () => {
      const s = await streaming();
      await s.send('and another thing');
      expect(queuedRows(get(s.messages))).toHaveLength(1);

      await s.selectRoom(2);
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 60 });
      await s.send("room 2's own turn");
      await flush();
      await emit('done');

      // Room 2's turn settled, so a drain ran — for room 2, whose queue is
      // empty. Room 1's message is untouched.
      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe("room 2's own turn");

      // Still there, and it goes out on the way back in — which is what
      // distinguishes "waited for the room" from "was never queued at all".
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 61 });
      await s.selectRoom(1);
      await flush();
      expect(api.sendChatMessage).toHaveBeenCalledTimes(2);
      expect(api.sendChatMessage.mock.calls[1][0]).toBe(1);
      expect(api.sendChatMessage.mock.calls[1][1]).toBe('and another thing');
    });

    it('withholds queued rows from the All view and gives them back after', async () => {
      const s = await streaming();
      await s.send('and another thing');

      await s.selectView('all');
      expect(queuedRows(get(s.messages))).toHaveLength(0);

      await s.selectRoom(1);
      await flush();
      // The room is idle by now, so it drains rather than re-rendering as
      // queued — either way the message came back rather than being lost.
      const mine = get(s.messages).filter((m) => m.text === 'and another thing');
      expect(mine).toHaveLength(1);
    });
  });

  describe('a !command alongside the queue', () => {
    it('answers inline and leaves the queue where it is', async () => {
      const s = await streaming();
      await s.send('and another thing');
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: null });

      await s.send('!status');

      // The command went out now; the queued message did not.
      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('!status');
      const queued = queuedRows(get(s.messages));
      expect(queued.map((m) => m.text)).toEqual(['and another thing']);
      expect(queued[0].queueHeld).toBeUndefined();
      // And the running turn still owns the room.
      expect(get(s.status)).toBe('streaming');
    });

    it('queues an attachment-bearing message even when its text is a command', async () => {
      const s = await streaming();

      await s.send('!status', [attachment()]);

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRows(get(s.messages)).map((m) => m.text)).toEqual(['!status']);
    });
  });

  describe('a deleted room', () => {
    it('drops the room queue with the room', async () => {
      const s = await streaming();
      await s.send('and another thing');
      expect(queuedRows(get(s.messages))).toHaveLength(1);
      api.deleteChatRoom.mockResolvedValue({ ok: true });

      await s.deleteRoom(1);
      await flush();

      expect(queuedRows(get(s.messages))).toHaveLength(0);
      expect(api.sendChatMessage).not.toHaveBeenCalled();
    });

    it('keeps the queue when the delete is refused', async () => {
      const s = await streaming();
      await s.send('and another thing');
      api.deleteChatRoom.mockRejectedValue(new api.ChatRoomBusyError('busy'));

      await s.deleteRoom(1);

      expect(queuedRows(get(s.messages)).map((m) => m.text)).toEqual(['and another thing']);
    });
  });
});
