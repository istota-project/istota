/**
 * Answering a confirmation from the chat surface.
 *
 * The banner this file used to cover is gone. `PendingConfirmations.svelte`,
 * `GET /chat/confirmations` and the `pendingConfirmations` /
 * `refreshConfirmations` / `answerConfirmation` slice were the web answer to
 * ISSUE-241 — a question held by the inbound email gate belongs to no
 * transcript, so it had nothing to hang a card on. The notification inbox
 * carries the same question from every route in the app instead of only from
 * `/chat`, which is what the strip could never do; the store-level assertions
 * for it live in `notifications.test.ts` and the API-level ones in
 * `tests/test_confirmation_surfaces.py`.
 *
 * What is left here is the part that was never the banner's: answering with a
 * bare "yes" in the composer (ISSUE-243). The endpoint runs it inside the
 * request like a `!command` — no task, an inline result — but unlike a command
 * the exchange is *durable*: the server writes the answer and the ack into
 * `messages`, so both come back over the room stream. The ids it returns are
 * what stop that echo appending a second copy of each.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom } from '$lib/api';

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
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('./notices', () => notices);

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

function resetMocks() {
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
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
}

describe('chat store — the banner is gone', () => {
  beforeEach(resetMocks);

  it('exposes no confirmations slice', async () => {
    // Asserted rather than merely deleted: a store still publishing
    // `pendingConfirmations` would let a page mount a second answer path for a
    // question the inbox already owns, and answering from one would leave the
    // other stale — which is the failure the strip had against the
    // in-transcript card, reintroduced against the bell.
    const s = (await freshSession()) as unknown as Record<string, unknown>;
    expect(s.pendingConfirmations).toBeUndefined();
    expect(s.refreshConfirmations).toBeUndefined();
    expect(s.answerConfirmation).toBeUndefined();
  });

  it('does not poll a listing endpoint on entry or on the rooms tick', async () => {
    // The seed-on-entry call and the 30s rooms-reconciler call both went with
    // the slice. The bell's count is polled by the root layout, from every
    // route, and the room stream's `notifications` frame is the fast path.
    const s = await freshSession();
    await s.init();
    expect((api as Record<string, unknown>).listPendingConfirmations).toBeUndefined();
    s.teardown();
  });
});

describe('chat store — answering a confirmation from the composer', () => {
  beforeEach(resetMocks);

  afterEach(() => {
    vi.useRealTimers();
  });

  async function answeredSession() {
    const s = await freshSession();
    await s.init();

    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'Confirmed.',
      command_data: { kind: 'confirmation_answered', user_msg_id: 71, system_msg_id: 72 },
    });
    await s.send('yes');
    return s;
  }

  it('renders the ack and stamps both durable ids onto the rows', async () => {
    const s = await answeredSession();

    const rows = get(s.messages);
    const user = rows.find((m) => m.role === 'user');
    const ack = rows.find((m) => m.role === 'system');
    expect(user?.msgId).toBe(71);
    expect(ack?.text).toBe('Confirmed.');
    expect(ack?.msgId).toBe(72);
    s.teardown();
  });

  it('an ordinary inline command is untouched by the stamping', async () => {
    const s = await freshSession();
    await s.init();
    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'pong',
    });

    await s.send('!ping');

    const ack = get(s.messages).find((m) => m.role === 'system');
    expect(ack?.text).toBe('pong');
    expect(ack?.msgId).toBeUndefined();
    s.teardown();
  });
});
