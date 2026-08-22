/**
 * Web chat session engine.
 *
 * Owns rooms, the active room's message list, the in-flight task, and the
 * send / cancel / confirm / room actions. Streaming prefers SSE (EventSource)
 * and falls back to polling the snapshot endpoint when SSE is unavailable
 * (e.g. the mock dev backend, or a proxy that buffers event-streams).
 *
 * A single module-level instance is shared across the /chat surfaces.
 */
import { get, writable, type Writable } from 'svelte/store';
import { notifyError, notifySuccess, notifyWarning } from './notices';
import {
  cancelChatTask,
  chatStreamUrl,
  confirmChatTask,
  createChatRoom,
  deleteChatMessage,
  deleteChatRoom,
  ChatMessageBusyError,
  ChatRoomBusyError,
  getChatConfig,
  getChatMessagesView,
  getRoomMessages,
  getChatRooms,
  getRoomEvents,
  getTaskEvents,
  chatRoomStreamUrl,
  type ChatRoomEvent,
  listPendingConfirmations,
  type PendingConfirmation,
  listOutboundDrafts,
  approveOutboundDraft,
  discardOutboundDraft,
  editOutboundDraft,
  type OutboundDraft,
  markAllRoomsRead,
  markRoomRead,
  sendChatMessage,
  setChatMessageStarred,
  updateChatRoom,
  promoteChatRoom,
  type ChatAttachment,
  type ChatRoom,
  type ChatHistory,
  type ChatView,
  type ExternalTurnDisplay,
  type SendResult,
} from '$lib/api';
import { loadSetting, saveSetting } from '$lib/stores/persisted';
import { normalizeExternalTurnDisplay } from '$lib/stores/externalTurns';
import { sortRoomsByActivity, touchRoomActivity } from '$lib/stores/roomOrder';
import { resetCommandCatalogue } from '$lib/components/chat/autocomplete/providers';
import {
  applyEvent as applySegmentEvent,
  type ChatMessage,
  type Segment,
  type ToolEntry,
  type SearchResultsData,
  type SearchResultItem,
  type ConfirmationAnsweredData,
  type MessageReply,
} from '$lib/stores/segments';

// The message / segment model lives in the pure reducer module so it can be
// unit-tested without a DOM; re-export here so existing `$lib/stores/chat`
// importers keep working.
export type {
  ChatMessage,
  Segment,
  ToolEntry,
  SearchResultsData,
  SearchResultItem,
  ConfirmationAnsweredData,
  MessageReply,
};

/** Build an assistant message's `segments` from a finished task's history
 * payload. Tool entries render as neutral "done" chips (history carries no
 * per-tool success / progress / timing); the last text segment is the answer
 * (unsettled, prominent), all earlier text segments are settled narration. */
function historySegments(raw: { kind: string; text: string }[]): Segment[] {
  const segs: Segment[] = raw.map((s, i) => {
    if (s.kind === 'tool') {
      return {
        kind: 'tool',
        id: `h${i}`,
        tool: { id: `h${i}`, name: '', description: s.text, running: false },
      };
    }
    if (s.kind === 'thinking') {
      return { kind: 'thinking', id: `k${i}`, text: s.text, settled: true };
    }
    return { kind: 'text', id: `s${i}`, text: s.text, settled: true };
  });
  // Only the last *text* segment is the answer; thinking stays settled.
  for (let i = segs.length - 1; i >= 0; i--) {
    const s = segs[i];
    if (s.kind === 'text') {
      s.settled = false;
      break;
    }
  }
  return segs;
}

export type ChatStatus = 'idle' | 'sending' | 'streaming';

// Client-side ack verbs. The backend stamps its own verb in `task_started`,
// but that event can't arrive until the scheduler claims the task off its
// poll queue (a second or two cold). Seeding one of these the instant we
// create the placeholder removes the perceived "Thinking…" gap; the backend
// `task_started` verb is then skipped (see applyEvent) so the line doesn't
// flicker from one random verb to another. Real status (progress_text,
// tool_start) still takes over normally.
//
// This MUST mirror the master list in src/istota/events.py (PROGRESS_MESSAGES)
// so the client-side seed never shows a verb the backend wouldn't. Same verbs,
// only the trailing "..." rendered as a single "…". Keep the two lists in sync.
const ACK_VERBS = [
  'On it…',
  'Hmm…',
  'Heard, chef…',
  'Investigating…',
  'One sec…',
  'Copy that…',
  'Roger…',
  'Considering…',
  'Thinkifying…',
  'Braining…',
  'Improvising…',
  'Jamming…',
  'Riffing…',
  'Grooving…',
  'Beboppin’…',
  'Noodling…',
  'Syncopating…',
  'Comping…',
  'Soloing…',
  // Cephalopod
  'Inking…',
  'Tentacling…',
  'Suckering…',
  'Jetting…',
  'Unfurling…',
  'Chromatophoring…',
  'Squidding…',
  'Grasping…',
  'Probing…',
  'Siphoning…',
  // Cheeky
  'Instigating…',
  'Scheming…',
  'Concocting…',
  'Percolating…',
  'Marinating…',
  'Hatching…',
  'Sleuthing…',
  'Finagling…',
  'Wrangling…',
  'Tinkering…',
  'Rummaging…',
  'Conjuring…',
  'Fermenting…',
  'Machinating…',
  'Gallivanting…',
];

function randomAckVerb(): string {
  return ACK_VERBS[Math.floor(Math.random() * ACK_VERBS.length)];
}

// How long a send may stay open before its pending mark earns the screen.
//
// A send that resolves normally does so in well under 100ms, so an indicator
// shown unconditionally would flash for a frame on every message — noise that
// teaches you to ignore the one place a real problem would be reported. Past
// this, the send is slow enough that saying so is useful, and slow is also the
// state that precedes a failure.
const SEND_PENDING_GRACE_MS = 400;

// The 4xx that mean "later", not "no". Everything else in the range is a
// verdict on the request itself, so a retry of the same payload is futile.
// (429 arrives classified as `rate_limit` and never reaches this set.)
const TRANSIENT_4XX = new Set([408, 425, 429]);

// The cross-room aggregate views, in sidebar order. Also the validator for the
// persisted selection — anything else falls back to room mode.
const AGGREGATE_VIEWS: ChatView[] = ['all', 'unread', 'starred'];

const STREAM_KINDS = [
  'task_started',
  'tool_start',
  'tool_end',
  'tool_progress',
  'progress_text',
  'thinking',
  'text_delta',
  'context_management',
  'brain_fallback',
  'confirmation',
  'result',
  'error',
  'cancelled',
  'done',
];

/**
 * A send handed back to the composer, with the room it was typed in.
 *
 * Only `reply_target_gone` produces one — see `returnSend`. The attachments
 * travel because they are already uploaded: re-picking them would orphan the
 * first copies server-side and cost the user the work twice.
 */
export interface SendReturn {
  n: number;
  token: string | null;
  text: string;
  attachments: ChatAttachment[];
}

export interface ChatSession {
  rooms: Writable<ChatRoom[]>;
  activeRoomId: Writable<number | null>;
  messages: Writable<ChatMessage[]>;
  status: Writable<ChatStatus>;
  activeTaskId: Writable<number | null>;
  loaded: Writable<boolean>;
  // Cross-room aggregate views: 'room' renders the active room's live
  // transcript; the other three render a read-only stream across all member
  // rooms (no composer, no live streaming — reload on entry).
  view: Writable<'room' | ChatView>;
  selectView: (v: ChatView) => Promise<void>;
  // Star / unstar the durable message behind a transcript row (optimistic,
  // reverted on failure). No-op for rows without a msgId.
  toggleStar: (cid: number) => Promise<void>;
  // Hard-delete the durable message behind a transcript row. No-op for rows
  // without a msgId (a live placeholder isn't stored yet). The caller is
  // expected to have confirmed first — this does not prompt.
  deleteMessage: (cid: number) => Promise<void>;
  // Advance every room's web read cursor at once (header mark-all chip).
  markAllRead: () => Promise<void>;
  // Older-history paging (ISSUE-131): whether an older page exists, an
  // in-flight guard, and the fetch-and-prepend action the scroll handler calls.
  hasMore: Writable<boolean>;
  loadingOlder: Writable<boolean>;
  loadOlder: () => Promise<void>;
  init: () => Promise<void>;
  selectRoom: (id: number) => Promise<void>;
  selectRoomByToken: (token: string) => Promise<boolean>;
  // Jump-to-response: resolve a search result's turn (select room + page to it)
  // and signal the transcript to scroll. `scrollTarget` is the signal the route
  // watches to perform the DOM scroll + transient highlight.
  jumpToTask: (roomToken: string, taskId: number) => Promise<boolean>;
  // The same jump keyed on a canonical `messages.id` — what a rendered
  // citation clicks through to. A sibling of `jumpToTask` rather than a
  // parameter on it: only the resolution step differs.
  jumpToMsgId: (roomToken: string, msgId: number) => Promise<boolean>;
  scrollToCid: (cid: number) => void;
  scrollTarget: Writable<{ cid: number; nonce: number } | null>;
  newRoom: (name: string) => Promise<void>;
  renameRoom: (id: number, name: string) => Promise<void>;
  updateRoomSettings: (
    id: number,
    patch: { name?: string; model?: string | null; effort?: string | null },
  ) => Promise<void>;
  promoteRoom: (id: number) => Promise<void>;
  archiveRoom: (id: number) => Promise<void>;
  deleteRoom: (id: number) => Promise<void>;
  // The last send the backend acked: a monotonic counter plus the room it
  // belongs to. The composer holds the submitted text as a draft until this
  // fires — a failed row does not survive a reload and the stored draft does —
  // so it needs the ack as a signal, and it owns the key the draft is stored
  // under, which is why the room travels rather than the key.
  //
  // The room is what makes it safe. Two sends can be open at once (a room
  // switch resets `status` to 'idle', which un-gates the composer), and a bare
  // counter would let whichever acked first settle the other's draft — the
  // cross-turn leak Stage 3 removed from `pendingSend`, reintroduced here.
  sendSettled: Writable<{ n: number; token: string | null }>;
  // A send whose cited parent turned out to be gone: the text and attachments
  // go back to the composer, since Retry cannot resolve that failure. Same
  // counter-plus-room shape as `sendSettled`, and for the same reason.
  sendReturned: Writable<SendReturn>;
  send: (text: string, attachments?: ChatAttachment[], replyTo?: MessageReply) => Promise<void>;
  // Re-POST a failed send from its own row (ISSUE-200). Reuses the row rather
  // than appending a new one, so the canonical echo folds into it. No-op for a
  // row that didn't fail, or one whose failure a retry can't resolve.
  retrySend: (cid: number) => Promise<void>;
  cancel: () => Promise<void>;
  confirm: (cid: number, taskId: number) => Promise<void>;
  reject: (cid: number, taskId: number) => Promise<void>;
  // Questions waiting on the user that no room transcript can render: an
  // inbound email held by the untrusted-sender gate carries a synthetic thread
  // token, so it belongs to no room, and its body is withheld by design
  // (ISSUE-241). The banner above the transcript is the surface for those, and
  // it is what makes a web-only user able to answer at all.
  pendingConfirmations: Writable<PendingConfirmation[]>;
  refreshConfirmations: () => Promise<void>;
  answerConfirmation: (taskId: number, approve: boolean) => Promise<void>;
  // Outbound mail the approval gate is holding. User-scoped rather than
  // room-scoped — rooms are shared and a co-member must not see the body — so
  // the client places each card by the `room_token` on the draft.
  outboundDrafts: Writable<OutboundDraft[]>;
  refreshDrafts: () => Promise<void>;
  // The stream and the polling fallback both land here. Exposed because the
  // `unavailable` guard and the answered-suppression window are the two rules
  // that decide whether a card stays on screen, and neither is reachable
  // through the public verbs.
  applyDraftsSnapshot: (drafts: OutboundDraft[] | undefined, unavailable?: boolean) => void;
  answerDraft: (draftId: number, action: 'approve' | 'discard') => Promise<boolean>;
  editDraft: (draftId: number, body: string) => Promise<boolean>;
  // How much of an external-origin turn the transcript shows. Read from
  // `/chat/config` at init and edited on /settings, so it is server state
  // rather than a client-local preference — the reader may be on a phone one
  // day and a laptop the next, and how much of a stranger's mail they want
  // inline is a decision about the account, not about the browser. Seeded at
  // the default so the first paint is never `full` by accident.
  externalTurnDisplay: Writable<ExternalTurnDisplay>;
  teardown: () => void;
}

function createSession(): ChatSession {
  const rooms = writable<ChatRoom[]>([]);
  const activeRoomId = writable<number | null>(null);
  const messages = writable<ChatMessage[]>([]);
  const status = writable<ChatStatus>('idle');
  const activeTaskId = writable<number | null>(null);
  // Set when Stop is tapped before the send POST has returned a task id; applied
  // by `sendTurn` the moment it has one. See `cancel`.
  let cancelRequested = false;
  const loaded = writable(false);
  // Which pane the transcript renders: the active room, or a cross-room
  // aggregate view (All / Unread / Starred). Aggregate views are read-only
  // reading surfaces — no composer, no SSE; re-entering refreshes. The
  // selection is one thing at a time: entering a view deselects the room and
  // vice versa, and `init` restores whichever was last chosen.
  const view = writable<'room' | ChatView>('room');
  // Older-history paging (ISSUE-131). `oldestCursor` is the keyset to fetch the
  // next older page (raw stored created_at + id), `hasMore` whether one exists,
  // `loadingOlder` a re-entrancy guard the scroll handler reads. Reset per room.
  const hasMore = writable(false);
  const loadingOlder = writable(false);
  let oldestCursor: { ts: string; id: number } | null = null;
  function resetPaging() {
    oldestCursor = null;
    hasMore.set(false);
    loadingOlder.set(false);
  }

  let cidCounter = 0;
  const nextCid = () => ++cidCounter;
  let pollIntervalMs = 1500;
  // The single in-flight stream for the active room, plus a FIFO of tasks
  // waiting their turn. A room runs one task at a time (the backend's
  // per-channel claim gate serializes them), so the UI streams them in order:
  // start one, queue the rest, advance when the active one settles. Different
  // rooms run concurrently on the backend; switching rooms tears this down and
  // resumes from the new room's history.
  let activeStream: { stop: () => void } | null = null;
  let streamQueue: { taskId: number; cid: number }[] = [];
  // Bot-delivered messages (alerts / logs / notifications routed to the `web`
  // surface) arrive on the room stream as `role: 'system'` rows carrying a
  // notif_id. `seenNotifIds` dedups a streamed row against one the history
  // load already rendered; it's reset per room in loadHistory.
  const seenNotifIds = new Set<number>();
  // Slow metadata reconciler, NOT the live path — the room stream carries
  // content and unread deltas now. It is kept (rather than deleted) because
  // GET /chat/rooms is what drives the Talk→web read-state pull, which is
  // itself server-throttled at [web.chat] talk_read_sync_interval (60s); 30s
  // satisfies it comfortably. Do not remove this timer without moving that
  // pull somewhere else, or Talk read sync silently stops.
  let roomsTimer: ReturnType<typeof setInterval> | null = null;
  const ROOMS_REFRESH_MS = 30000;
  let onVisibility: (() => void) | null = null;

  // ---- Live room-event stream (live-web-chat-room-stream spec) ----
  //
  // One user-scoped SSE connection carries every message in every room the user
  // is a member of, whatever surface produced it. Room switching is a
  // client-side filter; background rooms get real content, not just a count.
  // Cursor is `messages.id` — one monotonic integer over user turns, assistant
  // turns and system rows.
  let roomStream: { stop: () => void } | null = null;
  // True only while an EventSource is actually open. Positive evidence that no
  // `messages` row can have been missed, which is what lets a recovery skip the
  // history reload (see `recoverStream`'s `metadataOnly`).
  let roomStreamLive = false;
  // Bumped by every `init()` and by `teardown()`, so a load interrupted
  // mid-flight abandons its remaining side effects instead of installing them
  // on a page the user has left.
  let initGeneration = 0;
  let roomCursor = 0;
  // The deletion tail's own cursor. Separate from `roomCursor` because a
  // delete is hard — there is no `messages` row left for the id-ordered event
  // tail to carry — so the server keeps a ledger and this tracks it. Passed
  // back on reconnect, or a message deleted while the tab was disconnected
  // would come back to life on the next resume.
  let roomDeletionCursor = 0;
  let lastRoomEventAt = Date.now();
  let hiddenSince: number | null = null;
  // Frames that land while a recovery reload is in flight. The reload's
  // `messages.set` would otherwise drop a row written after its DB read, and
  // the server's per-connection cursor has already moved past it so it will
  // never be re-sent. Buffer, then re-apply (dedup makes that idempotent).
  let recoveryBuffer: ChatRoomEvent[] | null = null;
  let recovering = false;
  // A recovery reload must not be able to wedge the live path. `applyRoomEvent`
  // buffers every frame while a reload is in flight and only `recoverStream`'s
  // `finally` releases it, so a request that never settles would swallow frames
  // forever and the `recovering` guard would refuse every future attempt.
  // `fetch` has no timeout of its own, so bound these three explicitly.
  const RECOVERY_FETCH_TIMEOUT_MS = 15000;
  // Frames for the room we are mid-send into. The canonical `messages` user row
  // is written by the POST before it returns — and, with user-scoped OAuth on,
  // before a bounded ~5s Talk mirror — so our own echo can arrive while the
  // bubble on screen still has no `task_id` to dedup against. Appending it then
  // produces a second user bubble AND (no assistant carries the id yet) a
  // second placeholder + task stream. Hold that room's frames for the duration
  // of the send and replay them once the id is stamped; the (role, task_id) key
  // then matches and the echo is dropped. Bounded by the POST, and scoped to
  // the one room, so nothing else is delayed.
  let pendingSend: { token: string; rows: ChatRoomEvent[] } | null = null;
  // Past roughly a minute of silence a reconnect has probably missed state the
  // stream does not carry — a star toggled on another device, a read cursor
  // advanced by the Talk→web sync, a membership change — so a reload is *more
  // correct*, not merely cheaper. Under a minute, a transparent patch beats a
  // flicker, and EventSource reconnects on ordinary blips often enough that
  // forcing a reload each time would be constant churn on a flaky network.
  const ROOM_STREAM_STALE_MS = 60000;
  const sendSettled = writable<{ n: number; token: string | null }>({ n: 0, token: null });
  const sendReturned = writable<SendReturn>({
    n: 0,
    token: null,
    text: '',
    attachments: [],
  });
  // Seeded at the default rather than left undefined: `init` may not have
  // answered before the first transcript paints, and the safe direction there
  // is to show less of a stranger's mail rather than more.
  const externalTurnDisplay = writable<ExternalTurnDisplay>('collapsed');

  // ---- Client-only rows -----------------------------------------------------
  //
  // A send the server never took exists nowhere but here. `messages` is
  // otherwise a projection of server history — `loadHistory` and `loadViewPage`
  // both rebuild it wholesale from the response — so a room switch, a
  // stream-recovery reload or a step into an aggregate view dropped the one
  // copy of the user's text along with the Retry that was its only way back.
  // A network outage triggers the reload and the failure at once, so the user
  // watched their message be reported as unsent and then vanish.
  //
  // Rows sit in this map only while they are off screen; a rebuild takes them
  // back out. Keyed by room, so a row is re-appended to the transcript it
  // belongs to and nowhere else.
  const strandedSends = new Map<string, ChatMessage[]>();

  // Client-only without ambiguity: a server row always carries a `msgId`.
  const isStranded = (m: ChatMessage) => m.sendState === 'failed' && m.msgId === undefined;

  /** Move whatever client-only rows are on screen into the holding map. */
  function stashStrandedSends() {
    for (const m of get(messages)) {
      if (!isStranded(m) || !m.roomToken) continue;
      const held = strandedSends.get(m.roomToken) ?? [];
      if (!held.some((x) => x.cid === m.cid)) held.push(m);
      strandedSends.set(m.roomToken, held);
    }
  }

  /**
   * `next`, with a room's client-only rows re-appended at the tail.
   *
   * The tail is where they were: a failed send is always the newest thing in
   * the room from this client's point of view, and its `createdAt` is later
   * than anything the server can return for that room.
   *
   * `token` names the room being rebuilt, or null for the All view, which
   * spans every room. Held rows are taken *out* of the map — they are back on
   * screen, and `stashStrandedSends` puts them away again on the way out.
   */
  function carryClientOnlyRows(
    prev: ChatMessage[],
    next: ChatMessage[],
    token: string | null,
  ): ChatMessage[] {
    const carried: ChatMessage[] = [];
    const seen = new Set(next.map((m) => m.cid));
    const take = (m: ChatMessage) => {
      if (!isStranded(m) || seen.has(m.cid)) return;
      if (token !== null && m.roomToken !== token) return;
      seen.add(m.cid);
      carried.push(m);
    };
    for (const m of prev) take(m);
    if (token === null) {
      for (const [key, held] of strandedSends) {
        held.forEach(take);
        strandedSends.delete(key);
      }
    } else {
      (strandedSends.get(token) ?? []).forEach(take);
      strandedSends.delete(token);
    }
    return carried.length ? [...next, ...carried] : next;
  }

  // Clone a segment (and its tool) so a keyed {#each} sees a fresh reference.
  // text/thinking are flat; only a tool segment has a nested object to clone.
  const cloneSeg = (s: Segment): Segment =>
    s.kind === 'tool' ? { ...s, tool: { ...s.tool } } : { ...s };

  const updateMsg = (cid: number, fn: (m: ChatMessage) => void) => {
    messages.update((arr) => {
      const idx = arr.findIndex((x) => x.cid === cid);
      if (idx === -1) return arr;
      const m = arr[idx];
      fn(m); // the reducer + helpers mutate the message in place
      // Rebuild references at every level — new array, new message object,
      // new segment + tool objects — so BOTH keyed `{#each}`s (the page's over
      // $messages, and Message's over segments) re-render. Svelte 5 treats a
      // same-reference keyed item as unchanged and skips its child, so an
      // in-place deep mutation (a streamed text append, the `result`
      // overwrite) never reaches the DOM — which is exactly why a full page
      // reload (rebuilds the array via messages.set) rendered correctly while
      // the live in-place stream froze after the first paint.
      const next = arr.slice();
      next[idx] = { ...m, segments: m.segments.map(cloneSeg) };
      return next;
    });
  };

  function applyEvent(cid: number, kind: string, payload: Record<string, any>) {
    updateMsg(cid, (m) => {
      if (kind === 'task_started') {
        // Generic "working on it" verb stamped by the executor (shared with
        // Talk). We already seeded a client-side verb when the placeholder
        // was created, so skip the overwrite to avoid a flicker from one
        // random verb to another — real status (progress_text / tool_start /
        // the first text delta) takes over via the reducer below.
        if (payload.text && !m.progress) m.progress = String(payload.text);
        return;
      }
      // Every other event kind builds the ordered segment list. The reducer
      // is pure and unit-tested in segments.test.ts.
      applySegmentEvent(m, kind, payload);
    });
  }

  function streamTask(taskId: number, cid: number): { stop: () => void } {
    let lastSeq = 0;
    let es: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let finished = false;
    // A task parked awaiting confirmation owns its room until the user acts —
    // hold the queue rather than advancing past it.
    let paused = false;

    // Stop the stream without touching the queue. Used both as the terminal
    // path (settle, below) and as the external "stop now" hook for room
    // switches / unmount.
    const halt = () => {
      if (finished) return;
      finished = true;
      if (es) {
        es.close();
        es = null;
      }
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    // Natural terminal: halt, then let the session advance to the next queued
    // task (or go idle) — unless we paused for a confirmation.
    const settle = () => {
      if (finished) return;
      halt();
      onStreamSettled(paused);
    };

    const handle = (kind: string, dataStr: string, seq: number) => {
      // Idempotent on seq. An SSE reconnect/replay (Last-Event-ID) or a brief
      // SSE↔poll overlap can redeliver an already-applied event; seq is
      // writer-assigned and monotonic per task, so anything at-or-below the
      // high-water mark is a duplicate. (Poll already fetches seq > lastSeq;
      // this guards the SSE branch too.) seq-less events (0) bypass the guard.
      if (seq) {
        if (seq <= lastSeq) return;
        lastSeq = seq;
      }
      let payload: Record<string, any> = {};
      try {
        payload = JSON.parse(dataStr);
      } catch {
        /* keep {} */
      }
      // A reducer/render throw must never wedge the stream — keep advancing
      // so later events (notably `result` / `done`) still apply.
      try {
        applyEvent(cid, kind, payload);
      } catch {
        /* swallow */
      }
      if (kind === 'confirmation') paused = true;
      // `done` is the normal terminal; settle on `error`/`cancelled` too so a
      // failure that arrives without a trailing `done` (older paths, dropped
      // connection) can't leave the room stuck on "Working…".
      if (kind === 'done' || kind === 'cancelled' || kind === 'error') settle();
    };

    const poll = async () => {
      if (finished) return;
      try {
        const { events } = await getTaskEvents(taskId, lastSeq);
        for (const ev of events) handle(ev.kind, JSON.stringify(ev.payload), ev.seq);
      } catch {
        /* transient; try again next tick */
      }
    };
    const startPolling = () => {
      if (pollTimer || finished) return;
      poll();
      pollTimer = setInterval(poll, pollIntervalMs);
    };

    try {
      es = new EventSource(chatStreamUrl(taskId), { withCredentials: true });
      for (const k of STREAM_KINDS) {
        es.addEventListener(k, (e: MessageEvent) => {
          // The browser fires a native 'error' event (no data) on the
          // EventSource for connection failures, which collides with our
          // server-sent `event: error` task error. Ignore the data-less
          // native one — es.onerror handles the fallback to polling.
          if (e.data == null) return;
          handle(k, e.data, Number(e.lastEventId) || 0);
        });
      }
      es.onerror = () => {
        if (finished) return;
        // SSE failed (or the mock backend isn't an event-stream): close it
        // and fall back to polling the snapshot endpoint.
        if (es) {
          es.close();
          es = null;
        }
        startPolling();
      };
    } catch {
      startPolling();
    }

    return { stop: halt };
  }

  // Start streaming `taskId` immediately. Caller guarantees no stream is active.
  function startStream(taskId: number, cid: number) {
    status.set('streaming');
    activeTaskId.set(taskId);
    activeStream = streamTask(taskId, cid);
  }

  // Stream now, or queue behind the active stream. Queued placeholders show a
  // "Queued…" line until their turn (task_started then stamps the real verb).
  function enqueueStream(taskId: number, cid: number) {
    if (activeStream) {
      // Insert in taskId order: ids are monotonic with backend execution
      // order, and concurrent send() POSTs can resolve out of order, so a
      // plain push could stream them in the wrong sequence.
      const at = streamQueue.findIndex((q) => q.taskId > taskId);
      if (at === -1) streamQueue.push({ taskId, cid });
      else streamQueue.splice(at, 0, { taskId, cid });
      updateMsg(cid, (m) => {
        if (!m.progress) m.progress = 'Queued…';
      });
      // A stream is still running — keep the room in the streaming state
      // (send() flipped it to 'sending' optimistically before the POST).
      status.set('streaming');
    } else {
      startStream(taskId, cid);
    }
  }

  // The active stream reached a terminal state. If it paused for a
  // confirmation, hold the queue (the user must confirm/reject first).
  // Otherwise advance to the next queued task, or go idle.
  function onStreamSettled(paused: boolean) {
    activeStream = null;
    if (!paused) {
      const next = streamQueue.shift();
      if (next) {
        startStream(next.taskId, next.cid);
        return;
      }
    }
    status.set('idle');
    activeTaskId.set(null);
    // A turn finished in the open room — its reply is now on screen, so mark
    // the room read (visibility-gated) before the user switches away.
    const rid = get(activeRoomId);
    if (rid != null) markActiveRead(rid);
  }

  // Halt the active stream and drop the queue without advancing — for room
  // switches and unmount. Remounting/reselecting resumes from history.
  function stopActive() {
    if (activeStream) {
      activeStream.stop();
      activeStream = null;
    }
    streamQueue = [];
    resetPaging();
    status.set('idle');
    activeTaskId.set(null);
    cancelRequested = false;
    // The single "this transcript is about to be replaced" hook — every caller
    // (selectRoom, selectView, teardown) clears `messages` right after this, so
    // rows that exist only on the client have to be put away here or they are
    // gone before the rebuild that would carry them ever runs.
    stashStrandedSends();
    // The echo buffer belongs to the turn that opened it, and this call has
    // just released the gates (`status` back to 'idle') that were supposed to
    // keep another turn from draining it. Abandoned rather than drained: those
    // frames are for a room the user has left, and `loadHistory` rebuilds that
    // room's transcript from the server on the way back in.
    pendingSend = null;
  }

  // Set a single room's unread badge locally (optimistic clears + merge).
  function setRoomUnread(id: number, n: number) {
    rooms.update((r) => r.map((x) => (x.id === id ? { ...x, unread_count: n } : x)));
  }

  // Persist "I've read this room up to now" — but only while the tab is
  // actually showing it (a background tab shouldn't eat the badge). The open
  // room's *display* is held at 0 by refreshRooms regardless; this call makes
  // that durable so the badge stays clear after switching away.
  function markActiveRead(roomId: number) {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    setRoomUnread(roomId, 0);
    markRoomRead(roomId).catch(() => {
      /* transient; next open/poll retries */
    });
  }

  // Re-fetch the room list and merge fresh unread counts (and any name/origin
  // backfill) into the existing entries by id — no drop of local state. The
  // active room is forced to 0 so looking at it always reads as clear, even if
  // a count lands before the mark-read round-trips.
  //
  // The merged list IS re-sorted by activity, which is the one thing this pass
  // used to refuse to do: the sidebar's order is a function of `last_activity`
  // now, so freezing the order here would strand a room whose stream frames the
  // client missed (a sleeping tab, a dropped connection) wherever it happened
  // to be. Reconciling that is the whole reason this poll survives.
  async function refreshRooms(timeoutMs = 0) {
    let list: ChatRoom[];
    try {
      ({ rooms: list } = await getChatRooms(timeoutMs));
    } catch {
      return;
    }
    const byId = new Map(list.map((r) => [r.id, r]));
    const active = get(activeRoomId);
    const unreadFor = (r: ChatRoom) => (r.id === active ? 0 : (r.unread_count ?? 0));
    rooms.update((cur) => {
      const seen = new Set<number>();
      const merged = cur.map((r) => {
        const fresh = byId.get(r.id);
        seen.add(r.id);
        if (!fresh) return r; // transiently absent — keep as-is
        return {
          ...r,
          name: fresh.name,
          origin: fresh.origin,
          talk_token: fresh.talk_token,
          // model/effort ride along so the header's model badge can't go stale
          // until reload when the default is changed on another device.
          model: fresh.model,
          effort: fresh.effort,
          unread_count: unreadFor(fresh),
          // Whichever stamp is newer. This response was built before it was
          // awaited, so a frame that landed in between is ahead of it — taking
          // the server's value unconditionally would drop a room the user is
          // watching back down the list until the next poll.
          last_activity:
            (fresh.last_activity ?? '') > (r.last_activity ?? '')
              ? fresh.last_activity
              : r.last_activity,
        };
      });
      // Append rooms that newly surfaced (e.g. a Talk room first mirrored in).
      for (const fresh of list) {
        if (!seen.has(fresh.id)) merged.push({ ...fresh, unread_count: unreadFor(fresh) });
      }
      return sortRoomsByActivity(merged);
    });
  }

  // ---- Pending confirmations (the cross-room banner) ----

  const pendingConfirmations = writable<PendingConfirmation[]>([]);

  async function refreshConfirmations() {
    try {
      const res = await listPendingConfirmations();
      pendingConfirmations.set(res.confirmations ?? []);
    } catch {
      // Never a notice: the banner is a supplement to whatever surface the
      // prompt was delivered on, and a failed poll is not a thing the user did.
      // The next tick retries.
    }
  }

  async function answerConfirmation(taskId: number, approve: boolean) {
    // Pessimistic removal: a card that vanished and came back would read as the
    // question having been asked twice.
    try {
      if (approve) await confirmChatTask(taskId);
      else await cancelChatTask(taskId);
    } catch {
      notifyError('Could not answer that request. Try again.', {
        key: 'chat:confirmation',
      });
      return;
    }
    pendingConfirmations.update((list) => list.filter((c) => c.task_id !== taskId));
    // The approved task starts running in a room the user may be watching; the
    // room stream carries it from here.
    void refreshRooms();
  }

  // ---- Held outbound drafts ----

  const outboundDrafts = writable<OutboundDraft[]>([]);

  // Ids answered in the last few seconds, and when. A snapshot is computed on a
  // worker thread on the room-check tick, so one read a moment before `release`
  // committed its claim arrives *after* the answer and puts the card back —
  // with a live Send button on mail already going out. Pressing it again is
  // harmless (`release` short-circuits on a `sent` row and returns the same
  // Message-ID), but the card reads as the send not having taken, which on an
  // irreversible action is the one thing this surface must not say. Bounded, so
  // a misclassified answer cannot hide held mail for longer than the window:
  // after it, the server's own view wins.
  const answeredAt = new Map<number, number>();
  const ANSWERED_SUPPRESS_MS = 20_000;

  function suppressAnswered(list: OutboundDraft[]): OutboundDraft[] {
    if (answeredAt.size === 0) return list;
    const now = Date.now();
    for (const [id, at] of answeredAt) {
      if (now - at > ANSWERED_SUPPRESS_MS) answeredAt.delete(id);
    }
    return answeredAt.size === 0 ? list : list.filter((d) => !answeredAt.has(d.id));
  }

  function dropDraftCard(draftId: number) {
    outboundDrafts.update((list) => list.filter((d) => d.id !== draftId));
  }

  /**
   * Drop a card and hold it down against an in-flight snapshot.
   *
   * Only for an answer the server **accepted**. On a refusal the row may still
   * be held, and suppressing there would hide answerable mail for the length of
   * the window — so those paths drop the card and let the re-read be the
   * authority, which is what puts it back if it is still there.
   */
  function forgetAnswered(draftId: number) {
    answeredAt.set(draftId, Date.now());
    dropDraftCard(draftId);
  }

  // Coalesces concurrent callers onto one request. A frame that stubs K drafts
  // makes K cards each ask for the full row, and the endpoint they ask is
  // deliberately un-budgeted — so without this the byte budget is "saved" by
  // fetching every body K times over.
  let draftsInFlight: Promise<void> | null = null;

  function refreshDrafts(): Promise<void> {
    if (draftsInFlight) return draftsInFlight;
    draftsInFlight = (async () => {
      try {
        const res = await listOutboundDrafts();
        outboundDrafts.set(suppressAnswered(res.drafts ?? []));
      } catch {
        // Same rule as the confirmations poll: a failed read is nothing the
        // user did, and the cards on screen stay until a read succeeds.
        // Clearing them on a transient failure would read as the mail having
        // gone out.
      } finally {
        draftsInFlight = null;
      }
    })();
    return draftsInFlight;
  }

  /**
   * Apply a drafts snapshot from the stream or the polling fallback.
   *
   * A whole-set replace, because that is what the server sends — and the set
   * shrinking is how a draft reports being sent or discarded elsewhere. Guarded
   * on `unavailable`, since a failed server-side read must leave the cards
   * alone rather than empty them.
   */
  function applyDraftsSnapshot(drafts: OutboundDraft[] | undefined, unavailable = false) {
    if (unavailable || !drafts) return;
    outboundDrafts.set(suppressAnswered(drafts));
  }

  /**
   * Approve or discard a held draft, returning whether it left the list.
   *
   * Removal is **optimistic on success only**, and the row is kept on every
   * failure: this card is the only place the held mail is visible in the web
   * UI, so dropping it on a refused approve would leave the user believing a
   * message went out that did not.
   *
   * A 409 is read against the action, not on its own. "Someone discarded this
   * elsewhere" settles a *discard* and is a refusal of a *send* — dropping the
   * card silently in the second case gives the user who pressed Send the same
   * feedback a successful send gives them, for mail that never left.
   */
  async function answerDraft(draftId: number, action: 'approve' | 'discard'): Promise<boolean> {
    let res;
    try {
      res =
        action === 'approve'
          ? await approveOutboundDraft(draftId)
          : await discardOutboundDraft(draftId);
    } catch {
      // An expired session throws `AuthError` out of the fetch wrapper. Without
      // this the button simply un-busies and nothing is said, on the one
      // surface where "nothing happened" is indistinguishable from "it worked".
      notifyError('Could not reach the server. Your message has not been sent.', {
        key: `chat:draft:${draftId}`,
      });
      return false;
    }
    if (res.ok) {
      forgetAnswered(draftId);
      return true;
    }
    const settledElsewhere =
      res.failure === 'gone' ||
      (res.failure === 'conflict' && (res.state === 'discarded' || res.state === 'sent'));
    if (settledElsewhere && action === 'discard') {
      // The row is already gone or already binned, which is what Discard was
      // for. Only this view was stale, so the card goes without a complaint.
      dropDraftCard(draftId);
      void refreshDrafts();
      return true;
    }
    if (settledElsewhere) {
      // The user pressed Send and nothing went out. Saying so is the whole
      // point: dropping the card silently here gives them exactly the feedback
      // a successful send gives.
      notifyError(
        res.state === 'sent'
          ? 'That message had already been sent.'
          : res.state === 'discarded'
            ? 'That message was discarded elsewhere, so it was not sent.'
            : 'That draft is no longer there, so nothing was sent.',
        { key: `chat:draft:${draftId}` },
      );
      dropDraftCard(draftId);
      void refreshDrafts();
      return false;
    }
    if (res.failure === 'sent_unrecorded') {
      // The mail left. Never a retry — see `DraftFailure`.
      notifyError(
        'That message was sent, but recording it failed. Check your Sent folder before resending.',
        { key: `chat:draft:${draftId}` },
      );
    } else if (res.failure === 'conflict') {
      // Either `sending`, or a 409 that named no state — both mean the row is
      // in motion and the card must stay.
      notifyError('That message is being sent right now.', {
        key: `chat:draft:${draftId}`,
      });
    } else {
      notifyError(res.error || 'Could not answer that draft.', {
        key: `chat:draft:${draftId}`,
      });
    }
    // The row's own state may have moved under us; re-read rather than guess.
    void refreshDrafts();
    return false;
  }

  /** Replace a held draft's body. The server returns the re-read row. */
  async function editDraft(draftId: number, body: string): Promise<boolean> {
    let res;
    try {
      res = await editOutboundDraft(draftId, body);
    } catch {
      notifyError('Could not save that edit.', { key: `chat:draft:${draftId}` });
      return false;
    }
    if (res.ok) {
      // The edit committed. A 2xx whose body did not parse is still a committed
      // edit, so it closes the editor and leaves the re-read to settle the
      // displayed text — reporting a failure there would tell the user their
      // correction was lost while the server holds it.
      if (res.draft) {
        const updated = res.draft;
        outboundDrafts.update((list) => list.map((d) => (d.id === draftId ? updated : d)));
      } else {
        void refreshDrafts();
      }
      return true;
    }
    notifyError(res.error || 'Could not save that edit.', {
      key: `chat:draft:${draftId}`,
    });
    void refreshDrafts();
    return false;
  }

  function startRoomsRefresh() {
    if (roomsTimer) return;
    roomsTimer = setInterval(() => {
      void refreshRooms();
      void refreshConfirmations();
    }, ROOMS_REFRESH_MS);
  }

  function stopRoomsRefresh() {
    if (roomsTimer) {
      clearInterval(roomsTimer);
      roomsTimer = null;
    }
  }

  const inFlight = (s?: string) => s === 'pending' || s === 'locked' || s === 'running';
  // A task that has not produced its final answer yet — in-flight, or parked
  // awaiting a confirmation the user must act on.
  const unsettled = (s?: string) => inFlight(s) || s === 'pending_confirmation';

  // ---- Room-stream frame handling ----

  // A burst of streamed rows would otherwise fire one mark-read POST each.
  // The cursor call is idempotent and the display is already held at 0, so
  // coalescing on a short window costs nothing and saves the round-trips.
  let lastStreamReadAt = 0;
  const STREAM_READ_THROTTLE_MS = 1000;
  function markActiveReadThrottled(roomId: number) {
    const now = Date.now();
    if (now - lastStreamReadAt < STREAM_READ_THROTTLE_MS) return;
    lastStreamReadAt = now;
    markActiveRead(roomId);
  }

  // Open a task stream for a turn that started on another surface (most often a
  // Talk turn under unified room sync) so its progress animates here too. The
  // task-events endpoint is ownership-gated, not source-gated, so the substrate
  // the web client already tails works unchanged. A `pending_confirmation` task
  // is picked up too — its persisted `confirmation` event replays and the card
  // renders, which the old poller skipped outright.
  function pickUpStreamedTask(taskId: number, status?: string) {
    if (get(activeTaskId) === taskId) return;
    if (streamQueue.some((q) => q.taskId === taskId)) return;
    if (get(messages).some((m) => m.role === 'assistant' && m.taskId === taskId)) return;
    const ph: ChatMessage = {
      cid: nextCid(),
      role: 'assistant',
      text: '',
      taskId,
      status,
      segments: [],
      streaming: true,
      createdAt: new Date().toISOString(),
    };
    messages.update((arr) => [...arr, ph]);
    enqueueStream(taskId, ph.cid);
  }

  // Append a streamed row to the open room's transcript, deduped three ways —
  // the durable id (a reload may already hold it), the (role, task_id) key our
  // own optimistic placeholders carry, and notif_id for system rows.
  function appendStreamedRow(row: ChatRoomEvent) {
    const cur = get(messages);
    if (typeof row.msg_id === 'number' && cur.some((m) => m.msgId === row.msg_id)) return;
    if (typeof row.task_id === 'number') {
      const mine = cur.find((m) => m.taskId === row.task_id && m.role === row.role);
      if (mine) {
        // Already on screen (our own send, or a placeholder being streamed
        // into). Stamp the durable star key so the row is starrable without a
        // reload, then drop the frame.
        const msgId = typeof row.msg_id === 'number' ? row.msg_id : null;
        const starred = !!row.starred;
        // For a user turn, adopt the canonical body too. The server does not
        // always store what was typed — an attachment-only send becomes a
        // descriptor, a `!model …` prefix is stripped — and without this the
        // web transcript would keep showing the raw text while Talk, a reload
        // and the LLM's own context all show the stored one. Never for an
        // assistant row: that text is the task stream's to build.
        const body = row.role === 'user' && typeof row.text === 'string' ? row.text : null;
        if ((msgId != null && mine.msgId !== msgId) || (body != null && body !== mine.text)) {
          updateMsg(mine.cid, (m) => {
            if (msgId != null) m.msgId = msgId;
            m.starred = starred;
            if (body != null) m.text = body;
          });
        }
        return;
      }
    }
    // A send we gave up on that the server had in fact accepted. A timeout, or
    // a socket dropped after the request was processed, leaves the row marked
    // failed with no task id — and its echo then arrives as a *second* bubble,
    // so the user sees the same message twice: once reported as unsent, once
    // being answered. Adopt the echo into the row it belongs to instead.
    //
    // Matched on the body, which is what makes it safe to claim a row: the
    // server rewrites a few sends (an attachment-only descriptor, a stripped
    // `!model` prefix), and those simply fall through to appending — the same
    // duplicate as before, rather than a wrong row being silently claimed.
    if (row.role === 'user' && typeof row.task_id === 'number' && typeof row.text === 'string') {
      const stranded = cur.find(
        (m) =>
          m.role === 'user' &&
          m.sendState === 'failed' &&
          m.taskId === undefined &&
          m.text === row.text,
      );
      if (stranded) {
        updateMsg(stranded.cid, (m) => {
          m.taskId = row.task_id!;
          if (typeof row.msg_id === 'number') m.msgId = row.msg_id;
          m.starred = !!row.starred;
          m.sendState = undefined;
          m.sendError = undefined;
          m.retryable = undefined;
          m.sendPayload = undefined;
          m.showSending = undefined;
        });
        // The turn is live after all, so pick up its stream the way a freshly
        // streamed user row would.
        if (unsettled(row.status)) pickUpStreamedTask(row.task_id, row.status);
        return;
      }
    }
    if (typeof row.notif_id === 'number') {
      if (seenNotifIds.has(row.notif_id)) return;
      seenNotifIds.add(row.notif_id);
    }
    messages.update((arr) => [...arr, buildHistoryMessage(row)]);
    if (row.role === 'user' && typeof row.task_id === 'number' && unsettled(row.status)) {
      pickUpStreamedTask(row.task_id, row.status);
    }
    // Content just landed in the room the user is looking at — persist the read
    // cursor past it (visibility-gated) so it doesn't resurface as unread.
    const rid = get(activeRoomId);
    if (rid != null) markActiveReadThrottled(rid);
  }

  // Background room: bump the unread badge. Rows stream for every member room,
  // so this is real content, not a count refetch.
  function bumpBackgroundRoom(roomId: number, row: ChatRoomEvent, countUnread = true) {
    if (!countUnread || row.role === 'user') return;
    // count_unread_messages excludes the user's own turns, so a turn mirrored
    // in from Talk must not ring its own room. `countUnread` is false for a row
    // a just-completed refreshRooms already counted.
    rooms.update((rs) =>
      rs.map((r) => (r.id === roomId ? { ...r, unread_count: (r.unread_count ?? 0) + 1 } : r)),
    );
  }

  // Keep the aggregate panes live instead of frozen snapshots. Starred is
  // skipped: a freshly arrived row is unstarred by definition. Unread applies
  // the same "not your own turn" rule as the badge math.
  function feedAggregateView(row: ChatRoomEvent) {
    const v = get(view);
    if (v === 'room' || v === 'starred') return;
    if (v === 'unread' && row.role === 'user') return;
    if (typeof row.msg_id === 'number' && get(messages).some((m) => m.msgId === row.msg_id)) return;
    messages.update((arr) => [...arr, buildHistoryMessage(row)]);
  }

  function applyRoomEvent(row: ChatRoomEvent, opts: { countUnread?: boolean } = {}) {
    if (recoveryBuffer) {
      recoveryBuffer.push(row);
      return;
    }
    const token = row.room_token;
    if (!token) return;
    // Every message row is activity in its room, whichever room that is and
    // whoever sent it — this is the single funnel every frame passes through,
    // so the sidebar's order stays live without a room refetch. Ahead of the
    // `pendingSend` buffer deliberately: a send's own echo is held back to
    // dedup the bubble, but the room it went to should rise immediately.
    rooms.update((rs) => touchRoomActivity(rs, token, row.created_at));
    if (pendingSend && token === pendingSend.token) {
      pendingSend.rows.push(row);
      return;
    }
    const room = get(rooms).find((r) => r.token === token);
    if (room && room.id === get(activeRoomId) && get(view) === 'room') {
      appendStreamedRow(row);
      return;
    }
    feedAggregateView(row);
    if (room) bumpBackgroundRoom(room.id, row, opts.countUnread ?? true);
  }

  // `message_deleted` frame: rows another client (or another tab) removed.
  // Applied to whatever is on screen — the room transcript and the aggregate
  // panes are one `messages` store, so one filter covers both. Unread badges
  // are deliberately left alone: they are the server's count and the 30s
  // reconciler settles them, and decrementing here would double-count against
  // a badge the deleting client's own read cursor may already have cleared.
  function applyDeletions(deletions: { msg_id: number }[]) {
    if (!deletions.length) return;
    const gone = new Set(deletions.map((d) => d.msg_id));
    messages.update((arr) => arr.filter((m) => m.msgId == null || !gone.has(m.msgId)));
  }

  // Replay the frames held for the duration of a send, now that the turn's
  // task id is on screen and the ordinary dedup can recognise our own echo.
  //
  // `expected` is the buffer the caller opened. A turn drains only its own:
  // the slot is one module-level reference, and a room switch between two
  // sends leaves whatever was open in it, so an unqualified drain releases
  // another turn's frames before that turn's task id has been stamped — which
  // is precisely the duplicate the buffer exists to prevent.
  type PendingSend = { token: string; rows: ChatRoomEvent[] };

  function drainPendingSend(expected?: PendingSend) {
    if (expected && pendingSend !== expected) return;
    const held = pendingSend;
    pendingSend = null;
    if (!held) return;
    for (const row of held.rows) {
      try {
        applyRoomEvent(row);
      } catch {
        /* one bad row must not strand the rest */
      }
    }
  }

  // `room` metadata frame: a rename / model / effort change, or a room
  // appearing or disappearing on another device or surface. Closes the
  // "renamed or deleted elsewhere never propagates" gap without a room refetch.
  function applyRoomFrame(frame: {
    action?: string;
    id?: number;
    room?: Partial<ChatRoom> & { id: number };
  }) {
    if (frame.action === 'remove') {
      const id = frame.id;
      if (typeof id !== 'number') return;
      forgetRoom(id);
      rooms.update((rs) => rs.filter((r) => r.id !== id));
      if (get(activeRoomId) === id) {
        const remaining = get(rooms);
        if (remaining[0]) void selectRoom(remaining[0].id);
        else {
          activeRoomId.set(null);
          messages.set([]);
        }
      }
      return;
    }
    const fresh = frame.room;
    if (!fresh || typeof fresh.id !== 'number') return;
    rooms.update((rs) => {
      const idx = rs.findIndex((r) => r.id === fresh.id);
      // The snapshot deliberately omits unread counts (they ride the `message`
      // frames), so merge rather than replace. It omits `last_activity` for the
      // same reason — it changes on every message, so diffing it would turn
      // every turn into a `room` frame — which leaves a room appearing here
      // with no stamp. Its appearance is itself the activity, so it takes one
      // now; the arriving message that caused it, and the 30s poll, both settle
      // it to the server's value.
      if (idx === -1) {
        const added = {
          ...(fresh as ChatRoom),
          unread_count: 0,
          last_activity: fresh.last_activity ?? new Date().toISOString(),
        };
        return sortRoomsByActivity([...rs, added]);
      }
      const next = rs.slice();
      next[idx] = {
        ...next[idx],
        name: fresh.name ?? next[idx].name,
        origin: fresh.origin ?? next[idx].origin,
        model: fresh.model ?? null,
        effort: fresh.effort ?? null,
      };
      return next;
    });
  }

  // Recovery routine shared by the server's `gap` frame and the client-side age
  // rule. Reloading is cheap AND authoritative — refreshRooms returns
  // server-computed unread counts for every room and the active room is one
  // 50-row page — so it is the right answer whenever replay is doubtful.
  // `cursor` is the server's max *scanned* id (null → ask for a fresh one).
  //
  // `metadataOnly` skips the transcript reload and reconciles the room list
  // alone. It is only ever passed when the SSE connection demonstrably stayed
  // open across the quiet period, which is positive evidence that no `messages`
  // row was missed — the stream delivered them — so the reload would buy
  // nothing and cost a visible flicker plus a restarted task stream.
  async function recoverStream(cursor: number | null, opts: { metadataOnly?: boolean } = {}) {
    if (recovering) return;
    recovering = true;
    recoveryBuffer = [];
    // Rows buffered before `refreshRooms` is issued are already in the DB the
    // server counts, so re-bumping them would inflate the badge. Rows arriving
    // after are counted locally — erring toward a duplicate rather than a lost
    // increment, and the 30s reconciler settles either way.
    let countedUpTo = 0;
    try {
      let target = cursor;
      if (target == null) {
        try {
          // limit=1 → the server does the cheap MAX(id) gate and hands back a
          // cursor without serializing a backlog we're about to discard.
          const seed = await getRoomEvents(roomCursor, 1, RECOVERY_FETCH_TIMEOUT_MS);
          target = seed.cursor;
          // The reload below re-reads from the server, which has already
          // dropped the deleted rows — so skip past them rather than replaying
          // deletions for messages that are no longer on screen.
          const d = Number(seed.deletion_cursor) || 0;
          if (d > roomDeletionCursor) roomDeletionCursor = d;
        } catch {
          target = null;
        }
      }
      const v = get(view);
      const rid = get(activeRoomId);
      // Every reload here is bounded: an unbounded one would hold the frame
      // buffer open indefinitely (see RECOVERY_FETCH_TIMEOUT_MS). The abort
      // also means a late response can never land on top of whatever replaced
      // it — the request is cancelled, not merely ignored.
      if (!opts.metadataOnly) {
        if (v === 'room' && rid != null) {
          stopActive();
          await loadHistory(rid, RECOVERY_FETCH_TIMEOUT_MS);
        } else if (v !== 'room') {
          await loadViewPage(v);
        }
      }
      countedUpTo = recoveryBuffer?.length ?? 0;
      await refreshRooms(RECOVERY_FETCH_TIMEOUT_MS);
      // The drafts frame is a diffed snapshot, so a change that happened during
      // the gap produced a frame the reconnecting client did not receive and no
      // later frame will repeat. Metadata-only recoveries need this too: a
      // backgrounded tab is exactly where an approval given on the phone would
      // otherwise leave a card on screen for a message already sent.
      void refreshDrafts();
      if (target != null && target > roomCursor) roomCursor = target;
    } catch {
      /* transient — the next frame or poll retries */
    } finally {
      const buffered = recoveryBuffer ?? [];
      recoveryBuffer = null;
      recovering = false;
      buffered.forEach((row, i) => {
        try {
          applyRoomEvent(row, { countUnread: i >= countedUpTo });
        } catch {
          /* one bad row must not strand the rest */
        }
      });
    }
  }

  function startRoomStream() {
    if (roomStream) return;
    let es: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let stopped = false;
    let opened = false;
    // Consecutive SSE errors while the browser is still retrying on its own.
    // Reconnect-for-free is one of the reasons this is SSE and not a WebSocket,
    // so an ordinary blip must not cost the connection — but a persistently
    // failing endpoint (a buffering proxy that accepts and then drops) has to
    // concede to polling eventually.
    let sseFailures = 0;
    const SSE_FAILURE_LIMIT = 3;
    // Once polling, re-probe SSE on this cadence. Unlike streamTask — where the
    // stream is short-lived and a permanent downgrade is harmless — this
    // connection is session-lived, so a single transient failure must not leave
    // the tab polling for the rest of the day.
    const SSE_RETRY_MS = 60000;
    let lastSseAttemptAt = 0;

    const closeEs = () => {
      roomStreamLive = false;
      if (es) {
        es.close();
        es = null;
      }
    };
    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };
    const halt = () => {
      stopped = true;
      closeEs();
      stopPolling();
    };

    // Polling fallback over the snapshot endpoint — the same shape streamTask
    // already uses when SSE is unavailable (mock dev backend, buffering proxy).
    const poll = async () => {
      if (stopped) return;
      try {
        const page = await getRoomEvents(roomCursor, 0, 0, roomDeletionCursor);
        lastRoomEventAt = Date.now();
        // Deletions first, and before the gap bail-out: a gap reloads the open
        // room from the server, which already omits the deleted rows, but the
        // cursor still has to advance or every poll re-sends the same batch.
        applyDeletions(page.deletions ?? []);
        applyDraftsSnapshot(page.drafts, page.drafts_unavailable === true);
        const delCursor = Number(page.deletion_cursor) || 0;
        if (delCursor > roomDeletionCursor) roomDeletionCursor = delCursor;
        if (page.gap) {
          if (page.cursor > roomCursor) roomCursor = page.cursor;
          void recoverStream(page.cursor);
          return;
        }
        for (const row of page.events) {
          try {
            applyRoomEvent(row);
          } catch {
            /* swallow */
          }
        }
        if (page.cursor > roomCursor) roomCursor = page.cursor;
      } catch {
        /* transient; try again next tick */
      } finally {
        maybeReconnect();
      }
    };
    const startPolling = () => {
      if (pollTimer || stopped) return;
      void poll();
      pollTimer = setInterval(() => void poll(), Math.max(pollIntervalMs, 1000));
    };

    // Try SSE again from the polling loop. Overlap is harmless: both paths are
    // idempotent on `roomCursor`, and polling stops as soon as a stream opens.
    const maybeReconnect = () => {
      if (stopped || es) return;
      if (Date.now() - lastSseAttemptAt < SSE_RETRY_MS) return;
      sseFailures = 0;
      connect();
    };

    function connect() {
      if (stopped || es) return;
      lastSseAttemptAt = Date.now();
      try {
        es = new EventSource(chatRoomStreamUrl(roomCursor, roomDeletionCursor), {
          withCredentials: true,
        });
      } catch {
        es = null;
        startPolling();
        return;
      }
      es.addEventListener('message', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        // Idempotent on the durable id: a Last-Event-ID resume or a brief
        // SSE↔poll overlap can redeliver a row we already applied.
        const id = Number(e.lastEventId) || 0;
        if (id) {
          if (id <= roomCursor) return;
          roomCursor = id;
        }
        let row: ChatRoomEvent;
        try {
          row = JSON.parse(e.data);
        } catch {
          return;
        }
        try {
          applyRoomEvent(row);
        } catch {
          /* a render throw must never wedge the stream */
        }
      });
      es.addEventListener('gap', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        let cursor = 0;
        try {
          cursor = Number(JSON.parse(e.data).cursor) || 0;
        } catch {
          return;
        }
        if (cursor > roomCursor) roomCursor = cursor;
        void recoverStream(cursor);
      });
      es.addEventListener('room', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        try {
          applyRoomFrame(JSON.parse(e.data));
        } catch {
          /* swallow */
        }
      });
      // Auxiliary frame, and a whole-set snapshot rather than a tail — it
      // carries no SSE `id:` for the same reason `room` and `message_deleted`
      // do not: that cursor belongs to the message tail.
      es.addEventListener('drafts', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        try {
          applyDraftsSnapshot(JSON.parse(e.data).drafts);
        } catch {
          /* swallow */
        }
      });
      // Auxiliary frame — it carries no SSE `id:` (that cursor belongs to the
      // message tail), so the deletion cursor travels inside the payload.
      es.addEventListener('message_deleted', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        try {
          const payload = JSON.parse(e.data);
          applyDeletions(payload.deletions ?? []);
          const c = Number(payload.cursor) || 0;
          if (c > roomDeletionCursor) roomDeletionCursor = c;
        } catch {
          /* swallow */
        }
      });
      es.onopen = () => {
        sseFailures = 0;
        roomStreamLive = true;
        stopPolling(); // a re-probe succeeded — the stream is the live path again
        const idle = Date.now() - lastRoomEventAt;
        lastRoomEventAt = Date.now();
        // First open follows a fresh history load — nothing to recover.
        if (opened && idle > ROOM_STREAM_STALE_MS) void recoverStream(null);
        opened = true;
      };
      es.onerror = () => {
        if (stopped) return;
        sseFailures += 1;
        roomStreamLive = false;
        // readyState CONNECTING (0, per the spec constant) means the browser
        // has already scheduled its own retry; closing here would throw that
        // away and pre-empt exactly the free reconnect SSE was chosen for. Let
        // it try, up to the limit. Anything else — CLOSED, or an implementation
        // with no readyState at all — is fatal, so fall back at once.
        if (es?.readyState === 0 && sseFailures < SSE_FAILURE_LIMIT) return;
        closeEs();
        startPolling();
      };
    }

    connect();
    if (!es) startPolling();
    roomStream = { stop: halt };
  }

  function stopRoomStream() {
    // Release any recovery / send hold so a teardown mid-reload can't leave the
    // session permanently swallowing frames (both guards are module-singleton
    // state, so a route remount would otherwise inherit the wedge).
    recovering = false;
    recoveryBuffer = null;
    pendingSend = null;
    roomStreamLive = false;
    if (roomStream) {
      roomStream.stop();
      roomStream = null;
    }
  }

  function removeVisibilityListener() {
    if (onVisibility && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility);
    }
    onVisibility = null;
  }

  // Build a render-ready ChatMessage from a server history row. Shared by the
  // first load and the scroll-up older-page prepend so both reconstruct the
  // segment list identically (ISSUE-122 / ISSUE-131).
  function buildHistoryMessage(m: ChatHistory['messages'][number]): ChatMessage {
    // Rebuild the ordered segment list from the persisted trace so a finished
    // turn renders the same interleaved layout across reloads. Prefer the
    // server's ordered `segments`; fall back to the flat `tools` descriptions +
    // answer for an in-flight turn or an old payload. History has no per-tool
    // success/timing, so chips render a neutral "done" state. An in-flight
    // assistant turn starts empty — its resumed SSE rebuilds the segments live.
    let segments: Segment[] = [];
    if (m.role === 'assistant') {
      if (m.segments && m.segments.length) {
        segments = historySegments(m.segments);
      } else if (!inFlight(m.status)) {
        segments = historySegments([
          ...(m.tools ?? []).map((d) => ({ kind: 'tool', text: d })),
          ...(m.text ? [{ kind: 'text', text: m.text }] : []),
        ]);
      }
    }
    return {
      cid: nextCid(),
      role: m.role,
      text: m.text,
      taskId: m.task_id,
      status: m.status,
      confirmation: !!m.confirmation,
      segments,
      streaming: m.role === 'assistant' && inFlight(m.status),
      createdAt: m.created_at,
      durationSeconds: typeof m.duration_seconds === 'number' ? m.duration_seconds : undefined,
      model: typeof m.model === 'string' && m.model ? m.model : undefined,
      // Durable-store identity → the star affordance; room labels ride along
      // on aggregate-view rows.
      msgId: typeof m.msg_id === 'number' ? m.msg_id : undefined,
      starred: typeof m.msg_id === 'number' ? !!m.starred : undefined,
      roomToken: m.room_token,
      roomName: m.room_name,
      // Server-resolved author for a user row the viewer did not write.
      author: typeof m.author === 'string' && m.author ? m.author : undefined,
      // Provenance for a user row that entered from outside the room. Both are
      // set here and nowhere else, so history, the aggregate panes and the live
      // stream mark the same turns as external.
      origin: typeof m.origin === 'string' && m.origin ? m.origin : undefined,
      subject: typeof m.subject === 'string' && m.subject ? m.subject : undefined,
      // Persisted server-side, so the chip survives leaving the room and
      // coming back (the composer's names are long gone by then).
      attachments: m.attachments?.length ? m.attachments : undefined,
      attachmentPaths: m.attachment_paths?.length ? m.attachment_paths : undefined,
      // The single place the server's citation becomes a row field, which is
      // what makes history, the aggregate panes and the live stream agree —
      // all three build their rows through here.
      replyTo: m.reply_to
        ? m.reply_to.deleted
          ? { msgId: m.reply_to.msg_id, deleted: true }
          : {
              msgId: m.reply_to.msg_id,
              role: m.reply_to.role,
              excerpt: m.reply_to.excerpt,
              deleted: false,
            }
        : undefined,
    };
  }

  async function loadHistory(roomId: number, timeoutMs = 0) {
    const hist = await getRoomMessages(roomId, { timeoutMs });
    // A reload of the room already on screen (a stream recovery) still has its
    // client-only rows in `messages`; one reached via a room switch has them in
    // the holding map. Stashing first puts both cases in one place, so the
    // carry below is the only thing that has to know where they came from.
    const prev = get(messages);
    stashStrandedSends();
    const roomToken = get(rooms).find((r) => r.id === roomId)?.token ?? null;
    // taskId → cid for assistant placeholders, so an in-flight task's stream
    // binds to the message the server already laid out in order.
    const cidByTask = new Map<number, number>();
    // Reset the per-room dedup set, then record every notification already in
    // the transcript so the idle poller only appends ones that arrive later.
    seenNotifIds.clear();
    const msgs: ChatMessage[] = hist.messages.map((m) => {
      const cm = buildHistoryMessage(m);
      if (m.role === 'assistant' && typeof m.task_id === 'number') {
        cidByTask.set(m.task_id, cm.cid);
      }
      if (m.role === 'system' && typeof m.notif_id === 'number') {
        seenNotifIds.add(m.notif_id);
      }
      return cm;
    });
    // `null` means every room to `carryClientOnlyRows`, which is right for the
    // All view and wrong here — a room missing from `$rooms` would inherit
    // every other room's held rows. Carry nothing rather than everything.
    messages.set(roomToken ? carryClientOnlyRows(prev, msgs, roomToken) : msgs);
    // Seed paging state from the first-load response.
    oldestCursor = hist.oldest_cursor ?? null;
    hasMore.set(!!hist.has_more);
    loadingOlder.set(false);

    // Resume the room's in-flight tasks in order: the first streams, the rest
    // queue behind it. A leading pending_confirmation is left parked (its card
    // is shown) — the user must act before the queue moves.
    const actives = hist.active_tasks ?? (hist.active_task ? [hist.active_task] : []);
    for (const at of actives) {
      if (at.status === 'pending_confirmation') continue;
      let cid = cidByTask.get(at.id);
      if (cid == null) {
        const ph: ChatMessage = {
          cid: nextCid(),
          role: 'assistant',
          text: '',
          taskId: at.id,
          status: at.status,
          segments: [],
          streaming: true,
          createdAt: new Date().toISOString(),
        };
        messages.update((arr) => {
          arr.push(ph);
          return arr;
        });
        cid = ph.cid;
      }
      enqueueStream(at.id, cid);
    }
  }

  // Load (or reload) the first page of an aggregate view into the transcript.
  // Shared by selectView and the mark-all-read reload of an open Unread view.
  async function loadViewPage(v: ChatView) {
    try {
      const hist = await getChatMessagesView(v);
      // Switched away mid-fetch — drop the page.
      if (get(view) !== v) return;
      const prev = get(messages);
      stashStrandedSends();
      const next = hist.messages.map(buildHistoryMessage);
      // Only All spans every room, so only All can honestly show a failed send
      // from one. Unread and Starred are filtered panes a client-only row is
      // not a member of, so their rebuilds leave the held rows where they are —
      // the room's own transcript still gets them back.
      messages.set(v === 'all' ? carryClientOnlyRows(prev, next, null) : next);
      oldestCursor = hist.oldest_cursor ?? null;
      hasMore.set(!!hist.has_more);
    } catch {
      // A load failure would belong in the page's own banner, but chat has
      // none — the pane just renders empty, which is indistinguishable from a
      // room with nothing in it. A notice beats silence; giving chat a real
      // load-failure banner is the better fix and is ISSUE-200's territory.
      notifyError('Failed to load messages');
    }
  }

  // Enter an aggregate view: tear down the room's live machinery (stream,
  // queue, notif poll, paging state), deselect the room, and load the first
  // page. The rooms-refresh timer keeps running so sidebar badges stay live.
  async function selectView(v: ChatView) {
    stopActive();
    view.set(v);
    saveSetting('chat.view', v);
    activeRoomId.set(null);
    messages.set([]);
    await loadViewPage(v);
  }

  // Star/unstar a transcript row optimistically; revert on failure. In the
  // Starred view a successful unstar also removes the row (kept during flight
  // so a failure can revert in place) — mirrors the feeds starred view.
  async function toggleStar(cid: number) {
    const m = get(messages).find((x) => x.cid === cid);
    if (!m || typeof m.msgId !== 'number') return;
    const next = !m.starred;
    updateMsg(cid, (mm) => {
      mm.starred = next;
    });
    try {
      await setChatMessageStarred(m.msgId, next);
      if (!next && get(view) === 'starred') {
        messages.update((arr) => arr.filter((x) => x.cid !== cid));
      }
    } catch {
      updateMsg(cid, (mm) => {
        mm.starred = !next;
      });
      notifyError("Couldn't update star.");
    }
  }

  // Hard-delete a transcript row. Pessimistic, unlike `toggleStar`: a star
  // reverts cleanly, but a row removed optimistically and then restored would
  // reappear in the middle of the transcript after the user watched it go —
  // and the request is one round trip.
  async function deleteMessage(cid: number) {
    const m = get(messages).find((x) => x.cid === cid);
    if (!m || typeof m.msgId !== 'number') return;
    try {
      await deleteChatMessage(m.msgId);
    } catch (e) {
      notifyError(
        e instanceof ChatMessageBusyError
          ? 'That turn is still running — delete it once it finishes.'
          : "Couldn't delete the message.",
      );
      return;
    }
    messages.update((arr) => arr.filter((x) => x.cid !== cid));
    notifySuccess('Message deleted.', { key: 'chat:message-delete' });
  }

  // Mark every room read in one shot (the header chip). Badges zero locally on
  // success; an open Unread view reloads to its (likely empty) fresh state.
  async function markAllRead() {
    try {
      await markAllRoomsRead();
    } catch {
      notifyError("Couldn't mark all rooms read.");
      return;
    }
    rooms.update((r) => r.map((x) => ({ ...x, unread_count: 0 })));
    if (get(view) === 'unread') await loadViewPage('unread');
  }

  // Fetch the next older page and prepend it (scroll-up paging). The scroll
  // handler captures the scroll anchor before calling and restores it after the
  // store updates, so the viewport stays put. Never touches active_tasks /
  // enqueueStream — an older page carries no in-flight slot, and resuming one
  // here would double-stream a task.
  async function loadOlder() {
    const v = get(view);
    if (v !== 'room') {
      // Aggregate views page the cross-room endpoint. No aux/notif dedup
      // bands here — the durable store is the only source — but dedup by
      // msg_id anyway so a boundary anomaly can't double a row.
      if (!get(hasMore) || get(loadingOlder) || !oldestCursor) return;
      loadingOlder.set(true);
      try {
        const hist = await getChatMessagesView(v, { before: oldestCursor });
        if (get(view) !== v) return;
        const have = new Set<number>();
        for (const m of get(messages)) {
          if (typeof m.msgId === 'number') have.add(m.msgId);
        }
        const page = hist.messages
          .filter((m) => typeof m.msg_id !== 'number' || !have.has(m.msg_id))
          .map(buildHistoryMessage);
        if (page.length) messages.update((cur) => [...page, ...cur]);
        oldestCursor = hist.oldest_cursor ?? null;
        hasMore.set(!!hist.has_more);
      } catch {
        // Transient — leave the cursor untouched so the next scroll retries.
      } finally {
        loadingOlder.set(false);
      }
      return;
    }
    const roomId = get(activeRoomId);
    if (roomId == null || !get(hasMore) || get(loadingOlder) || !oldestCursor) return;
    loadingOlder.set(true);
    try {
      const hist = await getRoomMessages(roomId, { before: oldestCursor });
      // Switched rooms mid-fetch — drop the page rather than prepend it into
      // the wrong transcript.
      if (get(activeRoomId) !== roomId) return;
      // Dedup against what's already on screen by the same identity the server
      // dedups on: (role, taskId) for task-backed turns, notif_id for system
      // rows. The band tiling already prevents overlap; this guards a
      // created_at tie straddling the page boundary.
      const haveTask = new Set<string>();
      for (const m of get(messages)) {
        if (typeof m.taskId === 'number') haveTask.add(`${m.role}:${m.taskId}`);
      }
      const fresh = hist.messages.filter((m) => {
        if (typeof m.notif_id === 'number') {
          if (seenNotifIds.has(m.notif_id)) return false;
          seenNotifIds.add(m.notif_id);
          return true;
        }
        if (typeof m.task_id === 'number') return !haveTask.has(`${m.role}:${m.task_id}`);
        return true;
      });
      const page = fresh.map(buildHistoryMessage);
      if (page.length) messages.update((cur) => [...page, ...cur]);
      oldestCursor = hist.oldest_cursor ?? null;
      hasMore.set(!!hist.has_more);
    } catch {
      // Transient — leave the cursor untouched so the next scroll retries.
    } finally {
      loadingOlder.set(false);
    }
  }

  async function init() {
    // `onMount` does not await this and `onDestroy` calls teardown regardless,
    // so a navigation away mid-load would otherwise let the rest of init run
    // *after* teardown — starting a stream, a timer and a visibility listener
    // on a page the user has left, and leaking one more of each per remount
    // (only the newest listener is ever removed). Every await below is followed
    // by a generation check; teardown bumps the counter.
    const gen = ++initGeneration;
    const superseded = () => gen !== initGeneration;
    try {
      const cfg = await getChatConfig().catch(() => null);
      if (superseded()) return;
      if (cfg?.client_poll_interval_ms) pollIntervalMs = cfg.client_poll_interval_ms;
      // Normalized rather than adopted: the column takes any string a hand
      // edit puts in it, and an unrecognized value must read as the default
      // instead of leaving the transcript with no branch to take.
      externalTurnDisplay.set(normalizeExternalTurnDisplay(cfg?.external_turn_display));
      const { rooms: list } = await getChatRooms();
      if (superseded()) return;
      rooms.set(sortRoomsByActivity(list));
      // Seed the stream cursor BEFORE the history read, not after. A row
      // committed in between is then re-delivered by the stream and dropped by
      // the `msg_id` dedup; seeding afterwards would place it below the cursor
      // *and* outside the rendered page — and `markRoomRead` below would have
      // already consumed it, so it would not even show as unread. Same
      // capture-before-reload discipline `recoverStream` uses.
      // (limit=1 → the server answers from its MAX(id) gate, not a serialized
      // page.)
      try {
        const seed = await getRoomEvents(0, 1);
        roomCursor = seed.cursor;
        // Seed the deletion cursor too, from the same call: the history load
        // below already reflects every deletion so far, so replaying them as
        // frames would be pure noise.
        roomDeletionCursor = Number(seed.deletion_cursor) || 0;
      } catch {
        roomCursor = 0;
        roomDeletionCursor = 0;
      }
      if (superseded()) return;
      // Restore the last selection. An aggregate view is a selection in its own
      // right, not a mode layered over a room — restoring the room here while
      // `view` still said 'all' (the session is a module singleton, so `view`
      // outlives the route) left both highlighted in the sidebar and rendered
      // the room's history inside the aggregate pane.
      const savedView = loadSetting<string | null>('chat.view', null);
      const aggregate = AGGREGATE_VIEWS.includes(savedView as ChatView)
        ? (savedView as ChatView)
        : null;
      if (aggregate) {
        view.set(aggregate);
        activeRoomId.set(null);
        await loadViewPage(aggregate);
        if (superseded()) return;
      } else {
        view.set('room');
        const persisted = loadSetting<number | null>('chat.activeRoomId', null);
        const target = list.find((r) => r.id === persisted) ?? list[0];
        if (target) {
          activeRoomId.set(target.id);
          setRoomUnread(target.id, 0);
          await loadHistory(target.id);
          if (superseded()) return;
          markRoomRead(target.id).catch(() => {});
        }
      }
      loaded.set(true);
      startRoomStream();
      // Slow metadata reconciler (see ROOMS_REFRESH_MS) — the stream is the
      // live path.
      startRoomsRefresh();
      // A gate parked before this tab was open has no stream frame to arrive
      // on, so the banner is seeded on entry rather than only on the next tick.
      void refreshConfirmations();
      // Same reason, and one more: the drafts frame is *diffed* against a
      // baseline seeded empty, so an instance where the set has not changed
      // since the connection opened pushes no frame at all. Without this seed a
      // draft held before the tab opened would wait for the next change to
      // something else.
      void refreshDrafts();
      if (typeof document !== 'undefined') {
        removeVisibilityListener(); // never stack two
        onVisibility = () => {
          if (document.visibilityState !== 'visible') {
            hiddenSince = Date.now();
            return;
          }
          const away = hiddenSince == null ? 0 : Date.now() - hiddenSince;
          hiddenSince = null;
          const rid = get(activeRoomId);
          if (rid != null) markActiveRead(rid);
          // Client-side half of the gap threshold: only the client knows how
          // long it was away, only the server knows what the delta costs, so
          // each decides with what it has. Same recovery routine either way.
          // A connection that stayed open across the hidden period cannot have
          // missed a `messages` row, so that case reconciles metadata only —
          // otherwise every alt-tab during a long turn would tear down a
          // perfectly healthy task stream and re-render its answer from seq 0.
          if (away > ROOM_STREAM_STALE_MS) {
            void recoverStream(null, { metadataOnly: roomStreamLive });
          }
        };
        document.addEventListener('visibilitychange', onVisibility);
      }
    } catch {
      // Same exemption as loadViewPage above: no banner exists to put this in.
      notifyError('Failed to load chat');
    }
  }

  async function selectRoom(id: number) {
    if (get(activeRoomId) === id && get(view) === 'room') return;
    stopActive();
    view.set('room');
    saveSetting('chat.view', 'room');
    activeRoomId.set(id);
    saveSetting('chat.activeRoomId', id);
    setRoomUnread(id, 0); // optimistic — chip vanishes immediately on click
    messages.set([]);
    await loadHistory(id);
    markRoomRead(id).catch(() => {
      /* non-fatal; refresh/poll will retry */
    });
  }

  async function newRoom(name: string) {
    const room = await createChatRoom(name);
    rooms.update((r) => sortRoomsByActivity([...r, room]));
    await selectRoom(room.id);
  }

  // Both of these merge rather than replace: the PATCH response is the room's
  // own record and carries no `last_activity`, so adopting it wholesale would
  // strip the sidebar's sort key and drop a renamed room to the bottom of the
  // list. A rename is not activity, so the stamp is kept, not bumped.
  async function renameRoom(id: number, name: string) {
    const updated = await updateChatRoom(id, { name });
    rooms.update((r) => r.map((x) => (x.id === id ? { ...x, ...updated } : x)));
  }

  async function updateRoomSettings(
    id: number,
    patch: { name?: string; model?: string | null; effort?: string | null },
  ) {
    const updated = await updateChatRoom(id, patch);
    rooms.update((r) => r.map((x) => (x.id === id ? { ...x, ...updated } : x)));
  }

  async function promoteRoom(id: number) {
    try {
      const updated = await promoteChatRoom(id);
      rooms.update((r) => r.map((x) => (x.id === id ? { ...x, ...updated } : x)));
    } catch {
      notifyError("Couldn't open this room in Talk.");
    }
  }

  // A room the user has just deleted or hidden takes its unsent messages with
  // it: they were only ever going to be re-sent into that room, and holding
  // them would leak an entry nothing can reach — or, for a hidden Talk room
  // that the user's next message un-hides, resurrect them under a token that
  // has come back.
  //
  // Clearing the map is not enough on its own. The departed room's transcript
  // is still in `messages` at this point (`deleteRoom` reselects a neighbour,
  // and `selectRoom` clears `messages` only *after* `stopActive`), so the
  // reselect's own stash would put every one of these rows straight back.
  function forgetRoom(id: number) {
    const token = get(rooms).find((r) => r.id === id)?.token;
    if (!token) return;
    strandedSends.delete(token);
    messages.update((arr) => arr.filter((m) => !(isStranded(m) && m.roomToken === token)));
  }

  async function archiveRoom(id: number) {
    await updateChatRoom(id, { archived: true });
    forgetRoom(id);
    rooms.update((r) => r.filter((x) => x.id !== id));
    if (get(activeRoomId) === id) {
      const remaining = get(rooms);
      if (remaining[0]) await selectRoom(remaining[0].id);
      else {
        activeRoomId.set(null);
        messages.set([]);
      }
    }
  }

  async function deleteRoom(id: number) {
    try {
      await deleteChatRoom(id);
    } catch (e) {
      if (e instanceof ChatRoomBusyError) {
        notifyWarning('This room has a task in progress — wait for it to finish or cancel it.');
      } else {
        notifyError("Couldn't delete room.");
      }
      return;
    }
    // On success (or a 404 already-gone) drop it from the list, mirroring
    // archiveRoom's fall-through when the active room disappears.
    forgetRoom(id);
    rooms.update((r) => r.filter((x) => x.id !== id));
    if (get(activeRoomId) === id) {
      const remaining = get(rooms);
      if (remaining[0]) await selectRoom(remaining[0].id);
      else {
        activeRoomId.set(null);
        messages.set([]);
      }
    }
  }

  async function selectRoomByToken(token: string): Promise<boolean> {
    const room = get(rooms).find((r) => r.token === token);
    if (!room) return false;
    await selectRoom(room.id);
    return true;
  }

  // Jump-to-response (memory-search overhaul): a search result card asks the
  // transcript to scroll to a specific turn. The store owns resolution (select
  // the room, page history to find the turn); the DOM scroll + highlight is the
  // route's job, driven by the `scrollTarget` signal. The nonce lets a repeated
  // jump to the same cid re-fire the effect.
  const scrollTarget = writable<{ cid: number; nonce: number } | null>(null);
  let scrollNonce = 0;
  function scrollToCid(cid: number) {
    scrollTarget.set({ cid, nonce: ++scrollNonce });
  }

  // The cid of the (assistant, else any) transcript row for a task, or null.
  function findCidByTask(taskId: number): number | null {
    const msgs = get(messages);
    const assistant = msgs.find((m) => m.taskId === taskId && m.role === 'assistant');
    if (assistant) return assistant.cid;
    const any = msgs.find((m) => m.taskId === taskId);
    return any ? any.cid : null;
  }

  const JUMP_MAX_PAGES = 5;

  function findCidByMsgId(msgId: number): number | null {
    return get(messages).find((m) => m.msgId === msgId)?.cid ?? null;
  }

  // Select the target room (if needed), locate the row `resolve` names — paging
  // older history up to a bound when it's outside the loaded window — then
  // scroll to it. Returns false (and sets a transient error) on any miss rather
  // than throwing, so a stale/foreign link degrades gracefully.
  async function jumpToRow(roomToken: string, resolve: () => number | null): Promise<boolean> {
    try {
      const room = get(rooms).find((r) => r.token === roomToken);
      if (!room) {
        notifyError("Couldn't open that conversation.");
        return false;
      }
      if (get(activeRoomId) !== room.id || get(view) !== 'room') {
        const ok = await selectRoomByToken(roomToken);
        if (!ok) {
          notifyError("Couldn't open that conversation.");
          return false;
        }
      }
      let cid = resolve();
      let pages = 0;
      while (cid == null && get(hasMore) && !get(loadingOlder) && pages < JUMP_MAX_PAGES) {
        await loadOlder();
        pages += 1;
        cid = resolve();
      }
      if (cid == null) {
        notifyError("Couldn't locate that message.");
        return false;
      }
      scrollToCid(cid);
      return true;
    } catch {
      notifyError("Couldn't jump to that message.");
      return false;
    }
  }

  function jumpToTask(roomToken: string, taskId: number): Promise<boolean> {
    return jumpToRow(roomToken, () => findCidByTask(taskId));
  }

  /** The jump a rendered citation performs: same routine, keyed on the
   * canonical `messages.id` rather than on a task. */
  function jumpToMsgId(roomToken: string, msgId: number): Promise<boolean> {
    return jumpToRow(roomToken, () => findCidByMsgId(msgId));
  }

  async function send(text: string, attachments: ChatAttachment[] = [], replyTo?: MessageReply) {
    const roomId = get(activeRoomId);
    const trimmed = text.trim();
    if (!roomId || (!trimmed && attachments.length === 0)) return;

    const userCid = nextCid();
    // Which room this row belongs to, stamped now rather than waiting for the
    // echo: a send that fails has no echo, and the row has to be re-appendable
    // to its own transcript (and only its own) after a rebuild.
    const roomToken = get(rooms).find((r) => r.id === roomId)?.token;
    const idempotencyKey = newIdempotencyKey();
    messages.update((a) => [
      ...a,
      {
        cid: userCid,
        role: 'user',
        text: trimmed,
        segments: [],
        streaming: false,
        roomToken,
        attachments: attachments.map((x) => x.name),
        // The upload already told us where each file is reachable, so a chip
        // is a working link the moment it appears rather than only after the
        // turn comes back from history.
        attachmentPaths: attachments.map((x) => x.workspace_path ?? null),
        createdAt: new Date().toISOString(),
        // Rendered optimistically from what the composer staged, so the quote
        // is on the bubble the moment it appears; the echo replaces it with
        // the server's own resolution.
        replyTo,
        sendState: 'sending',
        // Stashed now rather than reconstructed on failure: the row keeps
        // display names and workspace paths, and a retry needs the host paths.
        sendPayload: {
          text: trimmed,
          attachments,
          idempotencyKey,
          replyToMsgId: replyTo?.msgId,
        },
      },
    ]);
    await runTurn(roomId, userCid, trimmed, attachments, idempotencyKey, replyTo?.msgId);
  }

  /**
   * A per-message identity the server dedups on, or undefined when the browser
   * cannot mint one.
   *
   * Undefined is a working send, not a failure: the endpoint treats a missing
   * key exactly as it did before the field existed. `crypto.randomUUID` is in
   * every target browser but is a secure-context API, and a send is not the
   * place to find out this page is not one.
   */
  function newIdempotencyKey(): string | undefined {
    try {
      return crypto.randomUUID();
    } catch {
      return undefined;
    }
  }

  async function retrySend(cid: number) {
    // Retry is the first entry into `runTurn` that isn't gated by the
    // composer's `busy`, and `runTurn` is still not re-entrant: it drains the
    // `pendingSend` slot on the way in, so retrying during another send would
    // release that send's echo before its task id was stamped. It also resets
    // `cancelRequested`, discarding a Stop tapped moments earlier.
    if (get(status) !== 'idle') return;
    const m = get(messages).find((x) => x.cid === cid);
    // Only a failed row that a retry could actually resolve. `retryable` is
    // false for an expired session, where re-POSTing would fail identically.
    if (!m || m.sendState !== 'failed' || m.retryable === false || !m.sendPayload) return;
    const roomId = get(activeRoomId);
    if (!roomId) return;
    const payload = m.sendPayload;
    updateMsg(cid, (mm) => {
      mm.sendState = 'sending';
      mm.sendError = undefined;
      mm.retryable = undefined;
    });
    // The rendered row's `attachmentPaths` still carry the workspace paths, so
    // the chips stay live across the retry; only the POST needs the host ones.
    //
    // The *original* key rides along rather than a fresh one: that is the whole
    // point of it. A send the server accepted and then failed to report (a
    // client timeout, a dropped socket) is recognised and answered with the
    // first task, so no second task and no second bubble exist to reconcile.
    await runTurn(
      roomId,
      cid,
      payload.text,
      payload.attachments,
      payload.idempotencyKey,
      payload.replyToMsgId,
    );
  }

  /**
   * The shared body of a first send and a retry: open the echo buffer, POST,
   * settle, then hand the turn to its assistant placeholder.
   *
   * Retry re-enters here with the *same* `userCid` deliberately — the echo
   * dedup in `appendStreamedRow` keys on `(role, task_id)`, so stamping the new
   * task id onto the existing row is what folds the canonical `messages` row
   * into it. Appending a fresh row would leave the failed one behind and show
   * two user bubbles for one message.
   */
  async function runTurn(
    roomId: number,
    userCid: number,
    trimmed: string,
    attachments: ChatAttachment[],
    idempotencyKey?: string,
    replyToMsgId?: number,
  ) {
    const phCid = nextCid();
    let graceTimer: ReturnType<typeof setTimeout> | undefined;
    // This turn's echo buffer, held locally so the drain below can name it.
    let mine: PendingSend | null = null;
    // The whole body is guarded, not just the POST: a throw anywhere in here
    // escaped to an un-awaited caller and left the row stuck on 'sending'
    // forever — and on a *retry* that is unrecoverable, since `retrySend`'s own
    // guard only accepts a row whose state is 'failed'.
    try {
      // The assistant placeholder is NOT appended here — `sendTurn` adds it on
      // the ack. Appended up front it spun its ack verb ("Sleuthing…") before
      // the message had reached the server, so once the grace below opened
      // `Sending…` the turn carried two progress indicators at once, and the
      // assistant one was claiming work that had not started. A `!command`
      // makes that certain rather than occasional: it runs inside the request,
      // so the POST stays open for the command's whole duration.
      status.set('sending');
      cancelRequested = false;

      // The mark's job is "this is taking longer than it should", not "a send
      // happened". The common send resolves in well under 100ms, and a mark that
      // appears and vanishes inside one frame is noise that trains you to ignore
      // it — so the row carries the truthful `sendState` immediately and the
      // render gate opens only if the POST is still open after the grace.
      //
      // With the placeholder deferred this is also the turn's *only* indicator
      // until the ack, which is what keeps the count at one: pre-ack the user
      // row owns it, post-ack the assistant row does.
      graceTimer = setTimeout(() => {
        updateMsg(userCid, (m) => {
          if (m.sendState === 'sending') m.showSending = true;
        });
      }, SEND_PENDING_GRACE_MS);

      // Hold this room's stream frames until the turn's task id is stamped
      // below — see `pendingSend`.
      //
      // The slot is empty by the time any turn reaches here, and the drain is
      // a safety net rather than a step: a turn's own `finally` clears it, a
      // room switch abandons it in `stopActive`, and the three ways to start a
      // turn are all gated against overlapping in the *same* room (the send
      // button is in Stop mode, `submit` refuses the keyboard chord while
      // busy, `retrySend` requires an idle status). What it must never do is
      // release a *live* turn's buffer, whose task id is not stamped yet —
      // that is the duplicate the buffer exists to prevent.
      drainPendingSend();
      const sendToken = get(rooms).find((r) => r.id === roomId)?.token;
      if (sendToken) {
        mine = { token: sendToken, rows: [] };
        pendingSend = mine;
      }

      await sendTurn(roomId, trimmed, attachments, userCid, phCid, idempotencyKey, replyToMsgId);
    } catch {
      // `sendChatMessage` classifies rather than throwing, so this is the
      // unforeseen case. It still must not escape: an un-reset 'sending' left
      // the composer locked in Stop mode until reload (ISSUE-200).
      //
      // Only when the send itself hadn't settled. `settleSend` runs the moment
      // the backend acks, ahead of everything downstream that could throw — so
      // a later failure is a problem with the *turn*, and reporting it as a
      // failed send would delete a placeholder whose task is genuinely running.
      // Past the ack the turn owns its own status transitions, so this leaves
      // them alone — forcing 'idle' here would strand a live stream by telling
      // the composer the room is free.
      if (get(messages).find((m) => m.cid === userCid)?.sendState === 'sending') {
        failSend(userCid, phCid, 'Couldn’t send — something went wrong.', true, roomId);
      }
    } finally {
      if (graceTimer) clearTimeout(graceTimer);
      updateMsg(userCid, (m) => {
        m.showSending = undefined;
      });
      // Runs after the task id is on both halves of the turn, so the replayed
      // echo dedups instead of duplicating. Only this turn's buffer: a room
      // switch (or a later send) may have abandoned or replaced the slot, and
      // draining that one would release frames whose turn has no id yet.
      if (mine) drainPendingSend(mine);
    }
  }

  /**
   * Attribute a send failure to the message that failed.
   *
   * The assistant placeholder is *removed* rather than repurposed as the error
   * surface. Writing "Failed to send" into it is what made a send failure read
   * as "the reply failed" — the misattribution ISSUE-200 is about. The turn
   * produced no assistant message, so it has no assistant row.
   */
  function failSend(
    userCid: number,
    phCid: number,
    reason: string,
    retryable: boolean,
    roomId: number,
  ) {
    messages.update((arr) => arr.filter((m) => m.cid !== phCid));
    updateMsg(userCid, (m) => {
      m.sendState = 'failed';
      m.sendError = reason;
      m.retryable = retryable;
      m.showSending = undefined;
    });
    // Only when this turn's room is still the one on screen. Switching rooms
    // isn't gated on `busy`, so a send failing after the switch would report
    // 'idle' about a room that may have a task streaming in it — unlocking the
    // composer, hiding Stop, and putting the next send into the backend's
    // per-channel gate. The row updates above no-op on their own (the failed
    // row left with its room).
    if (get(activeRoomId) === roomId) status.set('idle');
  }

  /**
   * Take the message back off the transcript and hand it to the composer.
   *
   * Only for `reply_target_gone`. Everything else leaves its failed row on
   * screen, because the row is the recovery path there; here it cannot be, so
   * leaving it would strand an un-retryable bubble in the transcript.
   */
  function returnSend(
    userCid: number,
    phCid: number,
    roomId: number,
    text: string,
    attachments: ChatAttachment[],
  ) {
    messages.update((arr) => arr.filter((m) => m.cid !== userCid && m.cid !== phCid));
    const token = get(rooms).find((r) => r.id === roomId)?.token ?? null;
    // The room travels with the counter for the same reason it does on
    // `sendSettled`: two sends can be open at once, and the composer must not
    // repopulate a room the text was not typed in.
    sendReturned.update((s) => ({ n: s.n + 1, token, text, attachments }));
    notifyWarning('That message is no longer available to reply to.');
    if (get(activeRoomId) === roomId) status.set('idle');
  }

  /**
   * The backend has the message: drop the send lifecycle off the row entirely.
   *
   * Absence is the settled state — the same state every row rebuilt from
   * history is in — so a delivered row is indistinguishable from a reloaded
   * one, and nothing downstream has to learn a third value.
   */
  function settleSend(userCid: number, roomId: number) {
    updateMsg(userCid, (m) => {
      m.sendState = undefined;
      m.showSending = undefined;
      m.sendError = undefined;
      m.retryable = undefined;
      m.sendPayload = undefined;
    });
    // The composer has been holding this message as a draft since it was
    // submitted — the stored draft is the only copy that survives a reload, so
    // it is dropped on the ack rather than on submit. This is the ack, and it
    // names the room so a second send open at the same time keeps its own.
    const token = get(rooms).find((r) => r.id === roomId)?.token ?? null;
    sendSettled.update((s) => ({ n: s.n + 1, token }));
  }

  /** The user-facing sentence for each way a send can fail. */
  function sendFailureReason(res: SendResult): { reason: string; retryable: boolean } {
    switch (res.failure) {
      case 'rate_limit':
        return {
          reason: `Rate limit reached — wait ${res.retry_after ?? 60}s and try again.`,
          retryable: true,
        };
      case 'unreachable':
        return { reason: 'Couldn’t send — the server is unreachable.', retryable: true };
      case 'timeout':
        return { reason: 'Couldn’t send — the server didn’t respond.', retryable: true };
      case 'auth':
        // No retry: re-POSTing with a dead session fails identically.
        return { reason: 'Your session expired. Reload to sign in again.', retryable: false };
      default:
        return {
          reason: res.error
            ? `Couldn’t send — ${res.error}.`
            : `Couldn’t send — the server returned ${res.status}.`,
          // A 4xx is a verdict on this request, so re-POSTing it unchanged
          // fails the same way — an archived room (409), a message over the
          // length cap (400), a body nginx refused (413). Offering Retry there
          // is the same lie the `auth` case is carved out to avoid. The two
          // 4xx that mean "later" keep it, matching the server's own split
          // (`PERMANENT_STATUS_CODES` in brain/claude_code.py).
          retryable: !(res.status >= 400 && res.status < 500) || TRANSIENT_4XX.has(res.status),
        };
    }
  }

  async function sendTurn(
    roomId: number,
    trimmed: string,
    attachments: ChatAttachment[],
    userCid: number,
    phCid: number,
    idempotencyKey?: string,
    replyToMsgId?: number,
  ) {
    const res = await sendChatMessage(
      roomId,
      trimmed,
      attachments.map((x) => x.path),
      attachments.map((x) => x.name),
      undefined,
      idempotencyKey,
      { replyToMsgId },
    );
    if (!res.ok) {
      // The one failure whose recovery is not Retry: the server rejected the
      // *citation*, so re-POSTing the same dead parent id fails identically.
      // The row is removed and the text handed back to the composer instead,
      // which contradicts the ISSUE-200 rule that a failed send never
      // repopulates the box — narrowly, and it earns it: this is a synchronous
      // pre-flight refusal, no time has passed in which anything else could
      // have been typed, and the alternative is a permanently un-retryable row.
      if (res.failure === 'reply_target_gone') {
        returnSend(userCid, phCid, roomId, trimmed, attachments);
        return;
      }
      const { reason, retryable } = sendFailureReason(res);
      // A `!command` runs inside the request rather than becoming a task, so
      // it returns before the endpoint ever consults the idempotency key — and
      // a timeout cannot distinguish "never arrived" from "ran, answer lost".
      // `!steer` appends a note per call and `!retry` creates a task per call,
      // so Retry is withheld for every command rather than guessing which are
      // safe to repeat. Same rule as the permanent 4xx above: an affordance
      // that would do the wrong thing is worse than none.
      failSend(userCid, phCid, reason, retryable && !trimmed.startsWith('!'), roomId);
      return;
    }
    // The backend acked, so the send itself is settled either way below. The
    // pending mark clearing is the ack's visible form; there is no receipt to
    // leave behind.
    settleSend(userCid, roomId);
    // Hand the turn over to its assistant row. Deferred to here rather than
    // appended before the POST so the transcript never carries two progress
    // indicators for one message — see `runTurn`.
    //
    // Guarded on the room, because this now runs after an await: `messages` is
    // rebuilt per room, so an unguarded append would drop this turn's spinner
    // into whichever transcript is on screen. The updates below then no-op on
    // their own (`updateMsg` is a no-op on an absent cid, and `enqueueStream`
    // streams into nothing) — the same already-tolerated state a room switch
    // produced before, when the switch wiped the placeholder out from under a
    // send in flight.
    if (get(activeRoomId) === roomId) {
      messages.update((a) => [
        ...a,
        {
          cid: phCid,
          role: 'assistant',
          text: '',
          segments: [],
          streaming: true,
          progress: randomAckVerb(),
          createdAt: new Date().toISOString(),
        },
      ]);
    }
    if (res.task_id == null) {
      // !command ran inline — no task, no stream.
      const cd = res.command_data as
        SearchResultsData | ConfirmationAnsweredData | null | undefined;
      updateMsg(phCid, (m) => {
        m.role = 'system';
        m.text = res.inline_result || '';
        // A structured search_results payload renders as result cards; any
        // other kind (or absent data) falls back to the markdown text.
        if (cd && cd.kind === 'search_results') m.searchResults = cd;
        m.progress = undefined;
        m.streaming = false;
      });
      if (cd && cd.kind === 'confirmation_answered') {
        // Unlike every other inline result, this one is *durable*: the server
        // wrote the answer and the ack into `messages`, so both echo back over
        // the room stream. Stamp their ids onto the two rows already on screen
        // — `appendStreamedRow` drops a frame whose `msg_id` is present — or
        // the exchange renders twice. This is also what makes the rows
        // starrable and deletable without a reload.
        const answered = cd as ConfirmationAnsweredData;
        if (typeof answered.user_msg_id === 'number') {
          updateMsg(userCid, (m) => {
            m.msgId = answered.user_msg_id!;
          });
        }
        if (typeof answered.system_msg_id === 'number') {
          updateMsg(phCid, (m) => {
            m.msgId = answered.system_msg_id!;
          });
        }
        // The banner above the transcript holds the same question. Clear it
        // now rather than at the next 30s tick — an answer that works while
        // the card lingers reads as the answer not having taken.
        void refreshConfirmations();
      }
      status.set('idle');
      return;
    }
    // Stamp the task id on BOTH halves of the turn. The assistant placeholder
    // needs it to bind its stream; the user bubble needs it so the room stream
    // recognises its own echo — the canonical `messages` user row arrives with
    // this task_id, and (role, task_id) is what dedups it away.
    updateMsg(userCid, (m) => {
      m.taskId = res.task_id!;
    });
    updateMsg(phCid, (m) => {
      m.taskId = res.task_id!;
      m.status = 'pending';
    });
    // A Stop tapped while this POST was in flight (see `cancel`) has an id to
    // act on now. Cancel first, then stream anyway: the stream is what renders
    // the cancellation as the turn's terminal state.
    if (cancelRequested) {
      cancelRequested = false;
      try {
        await cancelChatTask(res.task_id);
      } catch {
        /* ignore */
      }
    }
    // Stream now if the room is free, otherwise queue behind the in-flight
    // task. The backend gate keeps this task pending until its turn either way.
    enqueueStream(res.task_id, phCid);
  }

  async function cancel() {
    const taskId = get(activeTaskId);
    if (taskId == null) {
      // The turn is between the POST and its response, so there is no id to
      // cancel yet — but the composer has been showing Stop since `send` set
      // 'sending', and on a slow connection that window is long enough to tap
      // in. Latch the intent for `sendTurn` to apply against the real id rather
      // than dropping it silently. Gated on an in-flight send so a stray cancel
      // can never arm a later turn.
      if (get(status) === 'sending') cancelRequested = true;
      return;
    }
    try {
      await cancelChatTask(taskId);
    } catch {
      /* ignore */
    }
  }

  async function confirm(cid: number, taskId: number) {
    await confirmChatTask(taskId);
    updateMsg(cid, (m) => {
      m.confirmation = false;
      m.status = 'pending';
      // Drop the confirmation prompt's segments so the resumed stream
      // rebuilds the answer fresh (the prompt was a question, not the answer).
      m.segments = [];
      m.text = '';
      m.streaming = true;
      m.error = false;
    });
    // The confirmed task resumes ahead of anything queued behind it. The
    // stream paused (so no stream is active); enqueueStream starts it now.
    enqueueStream(taskId, cid);
  }

  async function reject(cid: number, taskId: number) {
    try {
      await cancelChatTask(taskId);
    } catch {
      /* ignore */
    }
    updateMsg(cid, (m) => {
      m.confirmation = false;
      m.status = 'cancelled';
      m.streaming = false;
      // Strike the declined prompt (the trailing text segment), or leave a
      // bare notice when there was none.
      const last = m.segments[m.segments.length - 1];
      if (last && last.kind === 'text' && last.text) last.text = `~~${last.text}~~`;
      else m.segments.push({ kind: 'text', id: 'declined', text: '_(declined)_', settled: false });
      m.text =
        m.segments[m.segments.length - 1].kind === 'text'
          ? (m.segments[m.segments.length - 1] as Extract<Segment, { kind: 'text' }>).text
          : '';
    });
    // The parked confirmation was holding the queue; release it so the next
    // queued message (if any) starts.
    onStreamSettled(false);
  }

  // Stop the active SSE / poll loop without cancelling the task. The route
  // calls this on unmount so navigating away from /chat doesn't leave an
  // EventSource (or poll timer) running; remounting re-subscribes from the
  // persisted task_events via loadHistory, so no progress is lost.
  function teardown() {
    // Invalidate any `init()` still in flight, so a navigation away mid-load
    // can't install a stream / timer / listener behind us.
    initGeneration += 1;
    stopActive();
    stopRoomStream();
    stopRoomsRefresh();
    // Drop the cached command/alias catalogue so a fresh session refetches it.
    resetCommandCatalogue();
    removeVisibilityListener();
  }

  return {
    rooms,
    activeRoomId,
    messages,
    status,
    activeTaskId,
    loaded,
    view,
    selectView,
    toggleStar,
    deleteMessage,
    markAllRead,
    hasMore,
    loadingOlder,
    loadOlder,
    init,
    selectRoom,
    selectRoomByToken,
    newRoom,
    renameRoom,
    updateRoomSettings,
    jumpToTask,
    jumpToMsgId,
    scrollToCid,
    scrollTarget,
    promoteRoom,
    archiveRoom,
    deleteRoom,
    sendSettled,
    sendReturned,
    send,
    retrySend,
    cancel,
    confirm,
    reject,
    pendingConfirmations,
    refreshConfirmations,
    answerConfirmation,
    outboundDrafts,
    refreshDrafts,
    applyDraftsSnapshot,
    answerDraft,
    editDraft,
    externalTurnDisplay,
    teardown,
  };
}

let _session: ChatSession | null = null;

export function getChatSession(): ChatSession {
  if (!_session) _session = createSession();
  return _session;
}
