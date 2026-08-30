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
import { MAX_QUEUED_PER_ROOM, SEND_QUEUE_STORAGE_KEY } from './sendQueue';

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

// Hoisted, so the same functions and the same backing store survive the
// `vi.resetModules()` in `freshSession()` — a restore test has to seed storage
// before the store module exists. `sendQueue.ts` imports this module by a
// relative path and `chat.ts` through the alias; both resolve to one file, so
// both get this mock. Values round-trip through JSON exactly as the real
// `localStorage` pair does, which is what makes a seeded map an honest stand-in
// for one written by a previous session.
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

// Imported rather than restated: the "nothing was written" assertion below is
// vacuous against a key nothing writes, so a literal that drifted from the
// module's own constant would pass whatever the code did.
const QUEUE_KEY = SEND_QUEUE_STORAGE_KEY;

/** One entry as a previous session would have left it in storage. */
function storedEntry(text: string, over: Record<string, unknown> = {}) {
  return { cid: 1, text, attachments: [], held: false, queuedAt: Date.now(), ...over };
}

/** Seed the whole stored map, as if written before this page load. */
function seedQueues(map: Record<string, unknown[]>, user = 'alice') {
  const keyed: Record<string, unknown[]> = {};
  for (const [token, entries] of Object.entries(map)) keyed[`${user}:room:${token}`] = entries;
  persisted.store.set(QUEUE_KEY, JSON.stringify(keyed));
}

/** What is stored for a room right now. */
function storedQueue(token: string, user = 'alice'): Record<string, unknown>[] {
  const raw = persisted.store.get(QUEUE_KEY);
  return raw ? (JSON.parse(raw)[`${user}:room:${token}`] ?? []) : [];
}

describe('chat store — the send queue', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    Object.values(notices).forEach((v) => v.mockReset());
    persisted.store.clear();
    persisted.loadSetting.mockClear();
    persisted.saveSetting.mockClear();
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500, user_id: 'alice' });
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

    it('refuses a message past the per-room cap rather than dropping it later', async () => {
      // `writeQueue` trims a room to its FIFO head, so an eleventh message
      // accepted into memory would sit on screen looking queued while the copy
      // that survives a reload was the one silently dropped.
      const s = await streaming();
      for (let i = 0; i < MAX_QUEUED_PER_ROOM; i++) await s.send(`message ${i}`);
      expect(queuedRows(get(s.messages))).toHaveLength(MAX_QUEUED_PER_ROOM);

      await s.send('one too many');

      expect(queuedRows(get(s.messages))).toHaveLength(MAX_QUEUED_PER_ROOM);
      expect(queuedRows(get(s.messages)).some((m) => m.text === 'one too many')).toBe(false);
      expect(notices.notifyError).toHaveBeenCalled();
      // Memory and storage agree, which is the point of refusing at all.
      expect(storedQueue('t1')).toHaveLength(MAX_QUEUED_PER_ROOM);
      expect(api.sendChatMessage).not.toHaveBeenCalled();
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

    it('holds the rest of the queue when a drained send cites a message that is gone', async () => {
      const s = await streaming();
      await s.send('first');
      await s.send('second');
      api.sendChatMessage.mockResolvedValue({
        ok: false,
        status: 409,
        failure: 'reply_target_gone',
      });

      await emit('done');

      // `returnSend` takes the row off the transcript and hands the text back
      // to the composer, so only the second is left — and it is held.
      const queued = queuedRows(get(s.messages));
      expect(queued.map((m) => m.text)).toEqual(['second']);
      expect(queued[0].queueHeld).toBe(true);
      expect(get(s.sendReturned).text).toBe('first');
    });

    it('does not hold the queue when an inline command fails', async () => {
      // A failed `!status` says nothing about the turn the queued messages
      // were written against, and holding there would strand them: the turn's
      // own `done` no longer drains a held queue.
      const s = await streaming();
      await s.send('and another thing');
      api.sendChatMessage.mockResolvedValueOnce({
        ok: false,
        status: 500,
        failure: 'rejected',
        error: 'boom',
      });

      await s.send('!status');

      const queued = queuedRows(get(s.messages));
      expect(queued).toHaveLength(1);
      expect(queued[0].queueHeld).toBeUndefined();
      // And it still goes out when the turn it was written against ends well.
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });
      await emit('done');
      expect(api.sendChatMessage.mock.calls.at(-1)?.[1]).toBe('and another thing');
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

    it('carries the citation back with an edited reply', async () => {
      const s = await streaming();
      await s.send('and another thing', [], { msgId: 7, role: 'assistant', excerpt: 'earlier' });
      const cid = queuedRows(get(s.messages))[0].cid;

      s.editQueued(cid);

      const returned = get(s.sendReturned);
      expect(returned.replyToMsgId).toBe(7);
      expect(returned.replyTo?.msgId).toBe(7);
    });

    it('refuses to edit an entry belonging to a room that is not open', async () => {
      // The page's restore returns early on a token mismatch, so taking the
      // entry apart first would delete the only copy of the message.
      const s = await streaming();
      await s.send('and another thing');
      const cid = queuedRows(get(s.messages))[0].cid;
      await s.selectRoom(2);
      const before = get(s.sendReturned).n;

      s.editQueued(cid);

      expect(get(s.sendReturned).n).toBe(before);
      // Still there when the room comes back.
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 92 });
      await s.selectRoom(1);
      await flush();
      expect(api.sendChatMessage.mock.calls.at(-1)?.[1]).toBe('and another thing');
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

    it('puts the restored row below the turn it is waiting on, not above it', async () => {
      // The queued message was typed *behind* the running turn, so its bubble
      // belongs under that turn's placeholder. `loadHistory` rebuilds the
      // client-only rows before it resumes the room's live tasks, so a
      // placeholder appended at the tail would sort under the message waiting
      // on it and read as though the answer came first.
      const s = await streaming();
      await s.send('and another thing');
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
      await s.selectRoom(1);
      await flush();

      const rows = get(s.messages);
      const placeholder = rows.findIndex((m) => m.role === 'assistant' && m.taskId === 42);
      const queued = rows.findIndex((m) => m.sendState === 'queued');
      expect(placeholder).toBeGreaterThanOrEqual(0);
      expect(queued).toBeGreaterThan(placeholder);
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
      // Room 1 is still working on re-entry, so the row comes back as a row
      // rather than draining — which is the property this test is about.
      api.getRoomMessages.mockImplementation(async (roomId: number) =>
        roomId === 1
          ? {
              messages: [],
              active_task: { id: 42, status: 'running' },
              active_tasks: [{ id: 42, status: 'running' }],
            }
          : { messages: [], active_task: null, active_tasks: [] },
      );

      await s.selectView('all');
      expect(queuedRows(get(s.messages))).toHaveLength(0);
      // Withheld, not discarded: the All view is the one rebuild that skips a
      // queued row, so it has to leave it in the holding map on the way past.
      expect(get(s.messages).some((m) => m.text === 'and another thing')).toBe(false);

      await s.selectRoom(1);
      await flush();

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      const queued = queuedRows(get(s.messages));
      expect(queued.map((m) => m.text)).toEqual(['and another thing']);
    });
  });

  describe('the drain conditions', () => {
    it('waits for a second task already queued behind the settling one', async () => {
      // Two live tasks in one room — a Talk turn adopted alongside our own, or
      // two resumed from history. The room is still busy when the first ends.
      const s = await freshSession();
      api.getRoomMessages.mockResolvedValue({
        messages: [],
        active_task: { id: 70, status: 'running' },
        active_tasks: [
          { id: 70, status: 'running' },
          { id: 71, status: 'running' },
        ],
      });
      await s.init();
      await s.selectRoom(1);
      await flush();
      expect(get(s.status)).toBe('streaming');
      await s.send('and another thing');
      expect(queuedRows(get(s.messages))).toHaveLength(1);

      // Task 70 finishes; task 71 takes the room, so nothing may drain yet.
      await emit('done');
      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRows(get(s.messages))).toHaveLength(1);

      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 72 });
      await emit('done');
      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('and another thing');
    });

    it('holds when the first of two live tasks is cancelled and the second ends done', async () => {
      // The hold is decided before the stream queue advances, or the second
      // turn's `done` would release a message written behind the abandoned one.
      const s = await freshSession();
      api.getRoomMessages.mockResolvedValue({
        messages: [],
        active_task: { id: 70, status: 'running' },
        active_tasks: [
          { id: 70, status: 'running' },
          { id: 71, status: 'running' },
        ],
      });
      await s.init();
      await s.selectRoom(1);
      await flush();
      await s.send('and another thing');

      await emit('cancelled');
      await emit('done');

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      const queued = queuedRows(get(s.messages));
      expect(queued).toHaveLength(1);
      expect(queued[0].queueHeld).toBe(true);
    });

    it('waits while an inline command is still in flight, then drains', async () => {
      // A command leaves its row 'sending' without claiming `status`, so the
      // room reads idle while a second send would still be concurrent.
      const s = await streaming();
      await s.send('and another thing');
      let releaseCommand: (v: unknown) => void = () => {};
      api.sendChatMessage.mockReturnValueOnce(
        new Promise((r) => {
          releaseCommand = r;
        }),
      );
      const commandDone = s.send('!status');
      await flush();

      // The turn settles while the command is still open: no drain.
      await emit('done');
      expect(get(s.status)).toBe('idle');
      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(queuedRows(get(s.messages))).toHaveLength(1);

      // The command settles and re-tests the conditions, so the queue is not
      // stranded waiting for a room switch.
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });
      releaseCommand({ ok: true, status: 200, task_id: null, inline_result: 'nothing running' });
      await commandDone;
      await flush();

      expect(api.sendChatMessage).toHaveBeenCalledTimes(2);
      expect(api.sendChatMessage.mock.calls[1][1]).toBe('and another thing');
      expect(queuedRows(get(s.messages))).toHaveLength(0);
    });
  });

  describe('drain triggers other than a room switch', () => {
    it('drains on a transcript rebuilt by init rather than by selectRoom', async () => {
      // The session is a module singleton and `teardown` leaves the queue
      // alone, so leaving /chat and coming back rebuilds the open room through
      // `loadHistory` directly. `recoverStream` does the same after a stale
      // reconnect — and that one halts the stream first, so the task's own
      // `settle` can never fire. A drain hung on `selectRoom` reaches neither,
      // and the entry has no trigger left at all.
      const s = await streaming();
      await s.send('and another thing');
      // The turn finished while the tab was away, so the reload finds nothing
      // running.
      api.getRoomMessages.mockResolvedValue({
        messages: [],
        active_task: null,
        active_tasks: [],
      });
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 90 });

      s.teardown();
      await s.init();
      await flush();

      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('and another thing');
    });

    it('advances the queue when a drained send resolves inline with no task', async () => {
      // The endpoint answers every `!word` inside the request; `send()` queues
      // any it cannot find in its catalogue. Such a body drains, comes back
      // with no task id, and so never produces a stream to settle.
      const s = await streaming();
      await s.send('!nope do the thing');
      await s.send('and another thing');
      api.sendChatMessage.mockResolvedValueOnce({
        ok: true,
        status: 200,
        task_id: null,
        inline_result: 'Unknown command',
      });
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 91 });

      await emit('done');
      await flush();

      expect(api.sendChatMessage).toHaveBeenCalledTimes(2);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('!nope do the thing');
      expect(api.sendChatMessage.mock.calls[1][1]).toBe('and another thing');
      expect(queuedRows(get(s.messages))).toHaveLength(0);
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

    it('drops the queue on archive, so a token that comes back does not send it', async () => {
      // `forgetRoom` is the one place a departed room's client-only rows go,
      // so archive and a `remove` frame from another device cannot diverge
      // from delete.
      //
      // Asserting the row is off screen would prove nothing: archiving the
      // active room reselects a neighbour, which rebuilds the transcript
      // anyway. The discriminating question is whether the *entry* survived,
      // and the way to ask it is to bring the token back — which is the exact
      // hazard `forgetRoom`'s own comment names.
      const s = await streaming();
      await s.send('and another thing');
      expect(queuedRows(get(s.messages))).toHaveLength(1);
      api.updateChatRoom.mockResolvedValue({ ...room(1), archived: true });

      await s.archiveRoom(1);
      await flush();
      expect(get(s.activeRoomId)).toBe(2);

      // Room 1 surfaces again — un-archived elsewhere, or a Talk room
      // re-mirrored — and the refresh re-appends it.
      api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
      await vi.advanceTimersByTimeAsync(30000);
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 80 });

      await s.selectRoom(1);
      await flush();

      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(queuedRows(get(s.messages))).toHaveLength(0);
    });

    it('keeps the queue when the delete is refused', async () => {
      const s = await streaming();
      await s.send('and another thing');
      api.deleteChatRoom.mockRejectedValue(new api.ChatRoomBusyError('busy'));

      await s.deleteRoom(1);

      expect(queuedRows(get(s.messages)).map((m) => m.text)).toEqual(['and another thing']);
    });
  });

  describe('persistence', () => {
    it('writes a queued message to storage the moment it is queued', async () => {
      // At enqueue rather than on unload, so a tab closed on a queued message
      // depends on catching no departure event.
      const s = await streaming();

      await s.send('and another thing');

      const stored = storedQueue('t1');
      expect(stored).toHaveLength(1);
      expect(stored[0].text).toBe('and another thing');
      expect(stored[0].held).toBe(false);
    });

    it('takes a drained entry out of storage', async () => {
      const s = await streaming();
      await s.send('first');
      await s.send('second');
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });

      await emit('done');

      expect(storedQueue('t1').map((e) => e.text)).toEqual(['second']);
      expect(queuedRows(get(s.messages))).toHaveLength(1);
    });

    it('records the hold a Stop applied', async () => {
      const s = await streaming();
      await s.send('and another thing');

      await emit('cancelled');

      expect(storedQueue('t1')[0].held).toBe(true);
      expect(queuedRows(get(s.messages))[0].queueHeld).toBe(true);
    });

    it('drops the stored copy with the room', async () => {
      const s = await streaming();
      await s.send('and another thing');
      expect(storedQueue('t1')).toHaveLength(1);
      api.deleteChatRoom.mockResolvedValue({ ok: true });

      await s.deleteRoom(1);
      await flush();

      expect(storedQueue('t1')).toEqual([]);
    });

    it('keeps the stored copy when the room is only archived', async () => {
      // Archive is recoverable and a `remove` frame can be another device's
      // edit, so neither may destroy text the user committed to sending. The
      // in-memory queue still goes — nothing can fire it in the meantime, and
      // a restore always re-holds.
      const s = await streaming();
      await s.send('and another thing');
      api.updateChatRoom.mockResolvedValue({ ...room(1), archived: true });

      await s.archiveRoom(1);
      await flush();

      expect(storedQueue('t1')).toHaveLength(1);
      expect(storedQueue('t1')[0].text).toBe('and another thing');
    });

    it('drops the stored copy when a queued message is removed', async () => {
      const s = await streaming();
      await s.send('and another thing');
      const cid = queuedRows(get(s.messages))[0].cid;

      s.removeQueued(cid);

      expect(storedQueue('t1')).toEqual([]);
      expect(queuedRows(get(s.messages))).toHaveLength(0);
    });

    it('queues in memory only when the backend publishes no user id', async () => {
      // The key needs the caller's own id, and an older backend does not send
      // one. The queue still works; it just does not survive a reload.
      api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
      const s = await streaming();

      await s.send('and another thing');

      expect(queuedRows(get(s.messages))).toHaveLength(1);
      expect(persisted.store.has(QUEUE_KEY)).toBe(false);
    });
  });

  describe('restoring a stored queue', () => {
    it('brings a stored queue back held, and sends nothing', async () => {
      // The turn it was written against is over and unobserved, and the user
      // is not watching the room they wrote it in — so a page load must never
      // fire it. The stored entry says `held: false`; the restore overrides.
      seedQueues({ t1: [storedEntry('written before the reload')] });
      const s = await freshSession();

      await s.init();
      await flush();

      const queued = queuedRows(get(s.messages));
      expect(queued).toHaveLength(1);
      expect(queued[0].text).toBe('written before the reload');
      expect(queued[0].queueHeld).toBe(true);
      expect(queued[0].roomToken).toBe('t1');
      expect(api.sendChatMessage).not.toHaveBeenCalled();
    });

    it('sends a restored entry once it is released', async () => {
      seedQueues({
        t1: [storedEntry('written before the reload', { idempotencyKey: 'stable-key' })],
      });
      const s = await freshSession();
      await s.init();
      await flush();
      api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 55 });

      await s.releaseQueued(queuedRows(get(s.messages))[0].cid);
      await flush();

      expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
      expect(api.sendChatMessage.mock.calls[0][1]).toBe('written before the reload');
      // The key is minted at enqueue precisely so it survives the round trip:
      // two tabs draining the same restored entry get one task.
      expect(api.sendChatMessage.mock.calls[0][5]).toBe('stable-key');
    });

    it('gives a restored entry its row only in the room being rendered', async () => {
      // The entry is restored for every room at init; the *row* is rebuilt in
      // `loadHistory`, so a background room's message appears when it is
      // entered rather than in whatever transcript is open.
      seedQueues({ t2: [storedEntry('for the other room')] });
      const s = await freshSession();
      await s.init();
      await flush();
      expect(queuedRows(get(s.messages))).toHaveLength(0);

      await s.selectRoom(2);
      await flush();

      const queued = queuedRows(get(s.messages));
      expect(queued.map((m) => m.text)).toEqual(['for the other room']);
      expect(queued[0].queueHeld).toBe(true);
      expect(api.sendChatMessage).not.toHaveBeenCalled();
    });

    it('leaves a queue stored for a room the user no longer has', async () => {
      // Nothing can render it and nothing can drain it, so it is not restored
      // — but it is not deleted either: an archived room, or a room list that
      // came back short, must not cost the text. The TTL collects it.
      seedQueues({ t9: [storedEntry('for a room that is gone')] });
      const s = await freshSession();

      await s.init();
      await flush();

      expect(queuedRows(get(s.messages))).toHaveLength(0);
      expect(api.sendChatMessage).not.toHaveBeenCalled();
      expect(storedQueue('t9')).toHaveLength(1);
    });

    it('does not restore another user of this browser profile', async () => {
      // A shared Talk room has one token across every member, which is why the
      // key carries the user: without it one person's unsent message would be
      // restored into the other's transcript.
      //
      // `bobby` rather than `bob` so the test can actually fail. The restore
      // takes the token by cutting this user's own prefix off the key, and
      // `'bobby:room:t1'` is exactly as long as `'alice:room:'` plus a token —
      // so with the prefix check dropped the cut lands on `t1`, a room this
      // user really is in, and the message is restored. A shorter name cuts to
      // rubbish and is refused by the room check for the wrong reason.
      seedQueues({ t1: [storedEntry('typed by somebody else')] }, 'bobby');
      const s = await freshSession();

      await s.init();
      await flush();

      expect(queuedRows(get(s.messages))).toHaveLength(0);
      expect(api.sendChatMessage).not.toHaveBeenCalled();
    });

    it('mints a fresh cid rather than reusing the one that was stored', async () => {
      // `cidCounter` starts over on every page load, so a stored cid collides
      // with a row this session is about to mint. The cid is a client-local
      // display key, not durable identity — and a collision means two rows
      // answering to one id in a keyed {#each}.
      seedQueues({ t1: [storedEntry('written before the reload', { cid: 2 })] });
      api.getRoomMessages.mockResolvedValue({
        messages: [
          { msg_id: 11, role: 'user', text: 'one', created_at: '2026-08-29T09:00:00Z' },
          { msg_id: 12, role: 'assistant', text: 'two', created_at: '2026-08-29T09:00:01Z' },
          { msg_id: 13, role: 'user', text: 'three', created_at: '2026-08-29T09:00:02Z' },
        ],
        active_task: null,
        active_tasks: [],
      });
      const s = await freshSession();

      await s.init();
      await flush();

      const cids = get(s.messages).map((m) => m.cid);
      expect(new Set(cids).size).toBe(cids.length);
      expect(queuedRows(get(s.messages))).toHaveLength(1);
    });

    it('does not duplicate a queue that is still live in memory', async () => {
      // The session outlives the page, so `init()` runs again on a remount
      // with the queue still in memory. Storage is that map's mirror, so
      // re-reading it over a live queue would double every entry.
      const s = await streaming();
      await s.send('and another thing');
      // The turn is still running when the page comes back, so nothing drains.
      api.getRoomMessages.mockResolvedValue({
        messages: [],
        active_task: null,
        active_tasks: [{ id: 42, status: 'running' }],
      });

      s.teardown();
      await s.init();
      await flush();

      expect(queuedRows(get(s.messages)).map((m) => m.text)).toEqual(['and another thing']);
      expect(storedQueue('t1')).toHaveLength(1);
    });
  });
});
