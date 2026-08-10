/**
 * Held outbound drafts in the chat store.
 *
 * The store's job here is narrow and the failure modes are asymmetric: a card
 * removed when it should not have been tells the user their message went out
 * when it did not, and a card left behind when it should have gone invites a
 * second send of a message already delivered. Everything below is one of those
 * two.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom, OutboundDraft } from '$lib/api';

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
  listPendingConfirmations: vi.fn(),
  listOutboundDrafts: vi.fn(),
  approveOutboundDraft: vi.fn(),
  discardOutboundDraft: vi.fn(),
  editOutboundDraft: vi.fn(),
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

function draft(over: Partial<OutboundDraft> = {}): OutboundDraft {
  return {
    id: 41,
    status: 'pending',
    room_token: 't1',
    task_id: 7,
    subject: 'Re: Invite',
    body: 'Wednesday at two works.',
    to: ['stranger@example.invalid'],
    ...over,
  };
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

/** Let the un-awaited `refreshDrafts()` an answer fires off actually land. */
const settle = () => new Promise((r) => setTimeout(r, 0));

describe('chat store — held outbound drafts', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v)
        (v as unknown as { mockReset(): void }).mockReset();
    });
    Object.values(notices).forEach((v) => v.mockReset());
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.getChatMessagesView.mockResolvedValue({ messages: [], has_more: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [], next_seq: 0 });
    api.listPendingConfirmations.mockResolvedValue({ confirmations: [] });
    api.listOutboundDrafts.mockResolvedValue({ drafts: [] });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  describe('seeding', () => {
    it('fetches the held set on entry', async () => {
      // The stream frame is a *diff* against a baseline seeded empty, so an
      // instance whose set has not changed since the connection opened pushes
      // no frame at all — a draft held before the tab opened would otherwise
      // wait for the next unrelated change.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();

      await s.init();

      expect(api.listOutboundDrafts).toHaveBeenCalled();
      expect(get(s.outboundDrafts).map((d) => d.id)).toEqual([41]);
    });

    it('leaves the set alone when the read fails', async () => {
      const s = await freshSession();
      await s.init();
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      await s.refreshDrafts();
      api.listOutboundDrafts.mockRejectedValue(new Error('offline'));

      await s.refreshDrafts();

      // Clearing here would read as the mail having been sent.
      expect(get(s.outboundDrafts)).toHaveLength(1);
      expect(notices.notifyError).not.toHaveBeenCalled();
    });
  });

  describe('answering', () => {
    it('removes the card once the send is accepted', async () => {
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({ ok: true, status: 200, message_id: '<a@b>' });

      const ok = await s.answerDraft(41, 'approve');

      expect(ok).toBe(true);
      expect(get(s.outboundDrafts)).toHaveLength(0);
    });

    it('removes the card on a discard', async () => {
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.discardOutboundDraft.mockResolvedValue({ ok: true, status: 200 });

      await s.answerDraft(41, 'discard');

      expect(get(s.outboundDrafts)).toHaveLength(0);
      expect(api.discardOutboundDraft).toHaveBeenCalledWith(41);
    });

    it('keeps the card when the send is refused', async () => {
      // This card is the only place the held mail is visible in the web UI.
      // Dropping it on a refusal leaves the user believing it went out.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({
        ok: false,
        status: 502,
        failure: 'transient',
        error: 'relay refused',
      });

      const ok = await s.answerDraft(41, 'approve');

      expect(ok).toBe(false);
      expect(get(s.outboundDrafts)).toHaveLength(1);
      expect(notices.notifyError).toHaveBeenCalled();
    });

    it('drops a discard whose row the server says is already gone', async () => {
      // Answered from Talk or another tab. The decision stands and it is the
      // one Discard was asking for, so there is nothing to complain about.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.discardOutboundDraft.mockResolvedValue({ ok: false, status: 404, failure: 'gone' });
      // The server no longer holds it, which is the whole reason for the 404.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [] });

      const ok = await s.answerDraft(41, 'discard');

      expect(ok).toBe(true);
      expect(get(s.outboundDrafts)).toHaveLength(0);
      expect(notices.notifyError).not.toHaveBeenCalled();
      // And the re-read that follows agrees, rather than putting it back.
      await settle();
      expect(get(s.outboundDrafts)).toHaveLength(0);
    });

    it('says nothing was sent when a send finds the row gone', async () => {
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({ ok: false, status: 404, failure: 'gone' });
      api.listOutboundDrafts.mockResolvedValue({ drafts: [] });

      const ok = await s.answerDraft(41, 'approve');

      expect(ok).toBe(false);
      expect(notices.notifyError.mock.calls[0][0]).toContain('nothing was sent');
    });

    it('lets the re-read restore a card the server still holds', async () => {
      // The removal is optimistic, and the server is the authority. A refusal
      // is deliberately *not* held down by the answered-suppression window —
      // that window is for an answer the server accepted, and applying it to a
      // refusal would hide answerable mail for its whole length.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({ ok: false, status: 404, failure: 'gone' });

      await s.answerDraft(41, 'approve');
      await settle();

      expect(get(s.outboundDrafts)).toHaveLength(1);
    });

    it('reports a send refused because the row was discarded elsewhere', async () => {
      // The user pressed Send and the mail did not go out. Dropping the card
      // silently gives them exactly the feedback a successful send gives.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({
        ok: false,
        status: 409,
        failure: 'conflict',
        state: 'discarded',
      });
      api.listOutboundDrafts.mockResolvedValue({ drafts: [] });

      const ok = await s.answerDraft(41, 'approve');

      expect(ok).toBe(false);
      expect(notices.notifyError.mock.calls[0][0]).toContain('not sent');
    });

    it('drops a discard whose row was already discarded, without a notice', async () => {
      // Same 409, opposite reading: this is what Discard was for, so only the
      // view was stale.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.discardOutboundDraft.mockResolvedValue({
        ok: false,
        status: 409,
        failure: 'conflict',
        state: 'discarded',
      });
      api.listOutboundDrafts.mockResolvedValue({ drafts: [] });

      const ok = await s.answerDraft(41, 'discard');

      expect(ok).toBe(true);
      expect(notices.notifyError).not.toHaveBeenCalled();
    });

    it('keeps the card when a 409 names no state at all', async () => {
      // "We do not know" must not fall into the branch that reports the action
      // as having worked.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.discardOutboundDraft.mockResolvedValue({
        ok: false,
        status: 409,
        failure: 'conflict',
      });

      const ok = await s.answerDraft(41, 'discard');

      expect(ok).toBe(false);
      expect(get(s.outboundDrafts)).toHaveLength(1);
      expect(notices.notifyError).toHaveBeenCalled();
    });

    it('reports a thrown request rather than failing silently', async () => {
      // An expired session throws out of the fetch wrapper. Without a catch the
      // button just un-busies and nothing is said.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockRejectedValue(new Error('Not authenticated'));

      const ok = await s.answerDraft(41, 'approve');

      expect(ok).toBe(false);
      expect(get(s.outboundDrafts)).toHaveLength(1);
      expect(notices.notifyError.mock.calls[0][0]).toContain('has not been sent');
    });

    it('keeps a card whose row is mid-send, and says so', async () => {
      // The one 409 that is not a settled decision: the mail is going out right
      // now and the user must not be told it was discarded.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({
        ok: false,
        status: 409,
        failure: 'conflict',
        state: 'sending',
      });

      const ok = await s.answerDraft(41, 'approve');

      expect(ok).toBe(false);
      expect(get(s.outboundDrafts)).toHaveLength(1);
      expect(notices.notifyError.mock.calls[0][0]).toContain('being sent');
    });

    it('never offers a retry for mail that was sent but not recorded', async () => {
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({
        ok: false,
        status: 500,
        failure: 'sent_unrecorded',
        message_id: '<a@b>',
      });

      await s.answerDraft(41, 'approve');

      expect(notices.notifyError.mock.calls[0][0]).toContain('Sent folder');
    });
  });

  describe('snapshots from the stream', () => {
    it('replaces the set, so a draft answered elsewhere leaves', async () => {
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();

      await s.applyDraftsSnapshot([]);

      expect(get(s.outboundDrafts)).toHaveLength(0);
    });

    it('leaves the cards alone when the server could not read them', async () => {
      // The endpoint always sends the key, and `drafts_unavailable` is what
      // distinguishes an empty set from a failed read. Treating the two alike
      // would clear every card on one transient lock — and the snapshot runs on
      // a 2s busy timeout, so contention is the ordinary failure.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();

      await s.applyDraftsSnapshot([], true);

      expect(get(s.outboundDrafts)).toHaveLength(1);
    });

    it('does not resurrect a draft answered a moment ago', async () => {
      // The snapshot is computed on a worker thread on a 10s tick, so a read
      // taken just before the claim committed arrives after the answer. Putting
      // the card back would show a live Send button on mail already going out.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({ ok: true, status: 200 });
      await s.answerDraft(41, 'approve');

      await s.applyDraftsSnapshot([draft()]);

      expect(get(s.outboundDrafts)).toHaveLength(0);
    });

    it('carries a draft the user never answered', async () => {
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.approveOutboundDraft.mockResolvedValue({ ok: true, status: 200 });
      await s.answerDraft(41, 'approve');

      await s.applyDraftsSnapshot([draft(), draft({ id: 42 })]);

      expect(get(s.outboundDrafts).map((d) => d.id)).toEqual([42]);
    });
  });

  describe('refresh coalescing', () => {
    it('folds concurrent callers onto one request', async () => {
      // A frame that stubs K drafts makes K cards each ask for the full row,
      // and the endpoint they ask is deliberately un-budgeted — so without this
      // the frame's byte budget is "saved" by fetching every body K times.
      const s = await freshSession();
      await s.init();
      api.listOutboundDrafts.mockClear();
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });

      await Promise.all([s.refreshDrafts(), s.refreshDrafts(), s.refreshDrafts()]);

      expect(api.listOutboundDrafts).toHaveBeenCalledTimes(1);
    });

    it('allows a later refresh once the first has settled', async () => {
      const s = await freshSession();
      await s.init();
      api.listOutboundDrafts.mockClear();
      api.listOutboundDrafts.mockResolvedValue({ drafts: [] });

      await s.refreshDrafts();
      await s.refreshDrafts();

      expect(api.listOutboundDrafts).toHaveBeenCalledTimes(2);
    });
  });

  describe('editing', () => {
    it('replaces the row with the one the server re-read', async () => {
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.editOutboundDraft.mockResolvedValue({
        ok: true,
        status: 200,
        draft: draft({ body: 'Thursday, actually.' }),
      });

      const ok = await s.editDraft(41, 'Thursday, actually.');

      expect(ok).toBe(true);
      expect(get(s.outboundDrafts)[0].body).toBe('Thursday, actually.');
    });

    it('treats a 2xx carrying no row as the committed edit it is', async () => {
      // A truncated or proxy-mangled response still means the server stored the
      // text. Reporting a failure there tells the user their correction was
      // lost while the server holds it.
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.editOutboundDraft.mockResolvedValue({ ok: true, status: 200 });
      api.listOutboundDrafts.mockResolvedValue({
        drafts: [draft({ body: 'Thursday, actually.' })],
      });

      const ok = await s.editDraft(41, 'Thursday, actually.');

      expect(ok).toBe(true);
      expect(notices.notifyError).not.toHaveBeenCalled();
      await settle();
      expect(get(s.outboundDrafts)[0].body).toBe('Thursday, actually.');
    });

    it('reports a refused edit and leaves the stored row showing', async () => {
      api.listOutboundDrafts.mockResolvedValue({ drafts: [draft()] });
      const s = await freshSession();
      await s.init();
      api.editOutboundDraft.mockResolvedValue({
        ok: false,
        status: 409,
        failure: 'conflict',
        error: 'draft 41 changed state while being edited',
      });

      const ok = await s.editDraft(41, 'Thursday, actually.');

      expect(ok).toBe(false);
      expect(get(s.outboundDrafts)[0].body).toBe('Wednesday at two works.');
      expect(notices.notifyError).toHaveBeenCalled();
    });
  });
});
