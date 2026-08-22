/**
 * A `!command` sent while a turn is already in flight (ISSUE-300).
 *
 * The composer's mode gate used to make this unreachable, so the store never
 * had to consider it: `runTurn` is not re-entrant, and every path into it is
 * gated on the room being free. A command is the one message that does not need
 * a turn — it runs inside the request and comes back with no task id — so it
 * gets its own path here rather than an exemption from those guards.
 *
 * What that path must not touch is the live turn's state: `status`, the
 * `pendingSend` echo slot, and the pending-cancel flag all belong to the turn
 * that is running.
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
  fetchChatCommands: vi.fn(),
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

/**
 * A store whose module graph is fresh, with the command catalogue already
 * loaded into it.
 *
 * The catalogue is a per-session cache in the autocomplete providers, and
 * `resetModules` drops it along with everything else — so it is primed here,
 * through the same graph the store is imported from. Without it the store's own
 * check sees an empty catalogue and refuses every command, which is the correct
 * behaviour before the fetch lands and the wrong premise for these tests.
 */
const CATALOGUE = {
  commands: [
    { name: 'steer', help: 'Send a note into the running task' },
    { name: 'status', help: 'What is running' },
  ],
  model_aliases: [{ alias: 'opus', target: 'claude-opus-4-8', effort: null }],
};

async function freshSession(catalogue: typeof CATALOGUE = CATALOGUE) {
  vi.resetModules();
  api.fetchChatCommands.mockResolvedValue(catalogue);
  const providers = await import('$lib/components/chat/autocomplete/providers');
  await providers.loadCommandNames();
  const mod = await import('./chat');
  return mod.getChatSession();
}

/** A session with one turn streaming in room 1, task 42. */
async function streaming() {
  const s = await freshSession();
  await s.init();
  api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 42 });
  await s.send('hello');
  expect(get(s.status)).toBe('streaming');
  api.sendChatMessage.mockReset();
  return s;
}

describe('chat store — !command during a turn', () => {
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

  it('answers inline and leaves the running turn alone', async () => {
    const s = await streaming();
    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'Noted — steering the current task.',
    });

    await s.send('!steer check the other branch too');

    const msgs = get(s.messages);
    expect(msgs).toHaveLength(4);
    expect(msgs[2].role).toBe('user');
    expect(msgs[2].text).toBe('!steer check the other branch too');
    expect(msgs[2].sendState).toBeUndefined();
    expect(msgs[3].role).toBe('system');
    expect(msgs[3].text).toBe('Noted — steering the current task.');
    expect(msgs[3].streaming).toBe(false);
    // The turn that was already running still owns the room.
    expect(get(s.status)).toBe('streaming');
    expect(get(s.activeTaskId)).toBe(42);
  });

  it('refuses ordinary text, which would be a second turn', async () => {
    const s = await streaming();

    await s.send('second thought');

    expect(api.sendChatMessage).not.toHaveBeenCalled();
    expect(get(s.messages)).toHaveLength(2);
    expect(get(s.status)).toBe('streaming');
  });

  it('refuses a !word the server does not register as a command', async () => {
    // The catalogue is the only evidence available client-side. The server
    // would answer this one inline too ("Unknown command"), so the refusal is
    // conservatism rather than a claim about what the endpoint would do.
    const s = await streaming();

    await s.send('!nope do the thing');

    expect(api.sendChatMessage).not.toHaveBeenCalled();
    expect(get(s.messages)).toHaveLength(2);
  });

  it('refuses the literal !model prefix, which produces a task rather than an answer', async () => {
    // `!model <alias> <prompt>` is the one `!`-prefixed body the endpoint turns
    // into a task. It is refused because no command is registered under the
    // name `model` — not because `opus` is a model alias.
    const s = await streaming();

    await s.send('!model opus write me a poem');

    expect(api.sendChatMessage).not.toHaveBeenCalled();
  });

  it('does not refuse a command whose name is also a model alias', async () => {
    // Model aliases are not excluded from the command set: the endpoint
    // resolves `!model <alias>`, never a bare `!<alias>`, so an alias shadows
    // no command. Filtering on one would silently disable a real command that
    // happened to share a name with a role alias.
    const s = await freshSession({
      commands: [{ name: 'smart', help: 'A command that shares a role alias name' }],
      model_aliases: [{ alias: 'smart', target: 'claude-opus-4-8', effort: null }],
    });
    await s.init();
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 42 });
    await s.send('hello');
    api.sendChatMessage.mockReset();
    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'ran',
    });

    await s.send('!smart go');

    expect(api.sendChatMessage).toHaveBeenCalled();
  });

  it('refuses a command carrying an attachment', async () => {
    const s = await streaming();

    await s.send('!steer look at this', [attachment()]);

    expect(api.sendChatMessage).not.toHaveBeenCalled();
  });

  it('does not settle a send that has not been acked yet', async () => {
    // The window before the ack is the dangerous one: the live turn's echo slot
    // is still filling, and its status is 'sending' rather than 'streaming'. A
    // command that ran the ordinary path here would drain that slot and report
    // the room idle the moment its own inline answer arrived.
    const s = await freshSession();
    await s.init();
    let resolveSend: (v: unknown) => void = () => {};
    api.sendChatMessage.mockReturnValue(
      new Promise((r) => {
        resolveSend = r;
      }),
    );
    const sending = s.send('hello');
    await Promise.resolve();
    expect(get(s.status)).toBe('sending');

    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'still working',
    });
    await s.send('!status');

    expect(get(s.status)).toBe('sending');

    // And the turn underneath it completes normally.
    resolveSend({ ok: true, status: 200, task_id: 7 });
    await sending;
    expect(get(s.messages)[0].taskId).toBe(7);
    expect(get(s.status)).toBe('streaming');
  });

  it('reports a failed command without unlocking the composer', async () => {
    const s = await streaming();
    api.sendChatMessage.mockResolvedValue({
      ok: false,
      status: 500,
      failure: 'rejected',
      error: 'command blew up',
    });

    await s.send('!status');

    const msgs = get(s.messages);
    const row = msgs[msgs.length - 1];
    expect(row.role).toBe('user');
    expect(row.sendState).toBe('failed');
    // A command runs before the endpoint consults the idempotency key, so a
    // repeat is not safe to offer — same rule the ordinary send path applies.
    expect(row.retryable).toBe(false);
    expect(get(s.status)).toBe('streaming');
  });

  it('stamps a recorded steer so the room stream cannot echo it twice', async () => {
    // `cmd_steer` writes the note into `messages` as a `task_id IS NULL` user
    // row, which streams back carrying no task id — so `msg_id` is the only
    // dedup key `appendStreamedRow` has. Unstamped, one steer draws two user
    // bubbles live and one after a reload.
    const s = await streaming();
    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'Steering task #42…',
      command_data: { kind: 'steer_recorded', user_msg_id: 918, body: 'check the other branch' },
    });

    await s.send('!steer check the other branch');

    const msgs = get(s.messages);
    const row = msgs[msgs.length - 2];
    expect(row.role).toBe('user');
    expect(row.msgId).toBe(918);
    // Adopted from the server, so the live bubble reads as the reloaded one
    // will: the stored row holds the note, not the whole `!steer …` line.
    expect(row.text).toBe('check the other branch');
  });

  it('does not signal a draft ack, which belongs to the send still in flight', async () => {
    // `sendSettled` is what tells the composer to drop a stored draft, and it
    // names only the room. Two sends can be open in one room now, and the
    // command never held a draft — so signalling here would drop the *other*
    // send's stored copy, which if that send then fails is the only copy left.
    const s = await freshSession();
    await s.init();
    let resolveSend: (v: unknown) => void = () => {};
    api.sendChatMessage.mockReturnValue(
      new Promise((r) => {
        resolveSend = r;
      }),
    );
    const sending = s.send('hello');
    await Promise.resolve();
    const before = get(s.sendSettled).n;

    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'still working',
    });
    await s.send('!status');

    expect(get(s.sendSettled).n).toBe(before);

    // And the ordinary send still signals its own ack when it lands.
    resolveSend({ ok: true, status: 200, task_id: 7 });
    await sending;
    expect(get(s.sendSettled).n).toBe(before + 1);
  });

  it('leaves an unexpected task id to the room stream rather than claiming it', async () => {
    // Not expected to be reachable — `dispatch` answers every `!word` inline —
    // but if it happened during the pre-ack window `enqueueStream` would find
    // no live stream and make the command's task the *active* one, putting the
    // turn the user is watching behind it and pointing cancel at the wrong task.
    const s = await freshSession();
    await s.init();
    let resolveSend: (v: unknown) => void = () => {};
    api.sendChatMessage.mockReturnValue(
      new Promise((r) => {
        resolveSend = r;
      }),
    );
    const sending = s.send('hello');
    await Promise.resolve();

    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 99 });
    await s.send('!status');

    expect(get(s.activeTaskId)).not.toBe(99);

    resolveSend({ ok: true, status: 200, task_id: 7 });
    await sending;
    // The turn the user was watching owns the stream.
    expect(get(s.activeTaskId)).toBe(7);
  });

  it('still runs a command through the ordinary path when the room is free', async () => {
    const s = await freshSession();
    await s.init();
    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'nothing running',
    });

    await s.send('!status');

    const msgs = get(s.messages);
    expect(msgs).toHaveLength(2);
    expect(msgs[1].role).toBe('system');
    expect(msgs[1].text).toBe('nothing running');
    expect(get(s.status)).toBe('idle');
  });
});
