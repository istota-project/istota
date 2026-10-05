/**
 * A card under a private park's row (#624).
 *
 * The row is in the member's private room; the task it approves is in another
 * room. Answering it sends the room the card rendered in, opens no stream here,
 * and leaves this room's queue alone.
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

const PARKED_ROW = {
  role: 'system',
  text: 'Post this in Family as Istota?',
  notif_id: 9,
  msg_id: 9,
  created_at: '2026-10-04T10:00:00Z',
  about_room: { token: 'rm_family', name: 'Family' },
  confirmation: true,
  task_id: 31,
};

describe('chat store — a private park card', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 20 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({
      messages: [PARKED_ROW],
      active_task: null,
      active_tasks: [],
    });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [], next_seq: 0 });
    api.confirmChatTask.mockResolvedValue({ status: 'ok' });
    api.cancelChatTask.mockResolvedValue({ status: 'cancelling' });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  const card = (s: Awaited<ReturnType<typeof freshSession>>) =>
    get(s.messages).find((m) => m.role === 'system' && m.taskId === 31);

  it('confirms with the room it rendered in and does not hold the room', async () => {
    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(card(s)?.confirmation).toBe(true));

    await s.confirm(card(s)!.cid, 31);
    expect(api.confirmChatTask).toHaveBeenCalledWith(31, 't1');
    expect(card(s)!.confirmation).toBe(false);
    // No stream for another room's task, so the room is still free to send.
    expect(api.getTaskEvents).not.toHaveBeenCalled();
    expect(get(s.status)).toBe('idle');
    api.sendChatMessage.mockResolvedValue({ ok: true, task_id: 50 });
    await s.send('next thing');
    expect(api.sendChatMessage).toHaveBeenCalled();
    s.teardown();
  });

  it('keeps the card when the server refuses the room', async () => {
    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(card(s)?.confirmation).toBe(true));
    api.confirmChatTask.mockRejectedValue(
      new Error('Approve this from your private chat, where the full preview is shown.'),
    );

    await s.confirm(card(s)!.cid, 31);
    expect(card(s)!.confirmation).toBe(true);
    expect(get(s.status)).toBe('idle');
    s.teardown();
  });

  it('declining takes the card down and leaves the queue alone', async () => {
    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(card(s)?.confirmation).toBe(true));

    await s.reject(card(s)!.cid, 31);
    expect(api.cancelChatTask).toHaveBeenCalledWith(31);
    expect(card(s)!.confirmation).toBe(false);
    expect(card(s)!.status).toBeUndefined();
    s.teardown();
  });

  it('an email note with its task and mail leaves the queue alone (stage 5)', async () => {
    api.getRoomMessages.mockResolvedValue({
      messages: [
        {
          role: 'system',
          text: 'Ana wrote on Book club:\n\nReply waiting for your approval.',
          notif_id: 10,
          msg_id: 10,
          created_at: '2026-10-04T10:00:00Z',
          about_room: { token: 'rm_thread', name: 'Book club' },
          task_id: 41,
          mail: { to: ['ana@example.com'], cc: [], state: 'held', body: 'Thursday.' },
        },
      ],
      active_task: null,
      active_tasks: [],
    });
    const s = await freshSession();
    await s.init();
    const noted = () => get(s.messages).find((m) => m.role === 'system' && m.taskId === 41);
    await vi.waitFor(() => expect(noted()?.mail?.state).toBe('held'));
    expect(noted()!.confirmation).toBe(false);
    // No stream for the thread's task and no hold: the room sends at once.
    expect(api.getTaskEvents).not.toHaveBeenCalled();
    expect(get(s.status)).toBe('idle');
    api.sendChatMessage.mockResolvedValue({ ok: true, task_id: 50 });
    await s.send('tell them yes');
    expect(api.sendChatMessage).toHaveBeenCalled();
    expect(get(s.messages).some((m) => m.queueHeld)).toBe(false);
    s.teardown();
  });

  it("carries an email note's parts and its incoming mail (ISSUE-644)", async () => {
    const receivedMail = {
      from: { name: '', address: 'ana@example.com' },
      to: [],
      cc: [],
      date: '',
      subject: 'Dinner',
      attachments: [],
      new_text: 'Friday?',
      rest: '',
      labels: {},
    };
    api.getRoomMessages.mockResolvedValue({
      messages: [
        {
          role: 'system',
          text: 'note body',
          notif_id: 11,
          msg_id: 11,
          created_at: '2026-10-04T10:00:00Z',
          about_room: { token: 'rm_thread', name: 'Book club' },
          task_id: 42,
          email_note: { header: 'Ana wrote on Book club', outcome: 'Replied.', remark: 'Hi.' },
          received_mail: receivedMail,
        },
        {
          role: 'system',
          text: 'an alert',
          notif_id: 12,
          msg_id: 12,
          created_at: '2026-10-04T10:01:00Z',
          received_mail: receivedMail,
        },
      ],
      active_task: null,
      active_tasks: [],
    });
    const s = await freshSession();
    await s.init();
    const row = (id: number) => get(s.messages).find((m) => m.msgId === id);
    await vi.waitFor(() => expect(row(11)?.emailNote?.remark).toBe('Hi.'));
    expect(row(11)!.receivedMail?.subject).toBe('Dinner');
    // A system row that is not an email note takes no incoming card.
    expect(row(12)!.emailNote).toBeUndefined();
    expect(row(12)!.receivedMail).toBeUndefined();
    s.teardown();
  });
});
