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
import {
  cancelChatTask,
  chatStreamUrl,
  confirmChatTask,
  createChatRoom,
  deleteChatRoom,
  ChatRoomBusyError,
  getChatConfig,
  getChatMessagesView,
  getRoomMessages,
  getChatRooms,
  getRoomEvents,
  getTaskEvents,
  chatRoomStreamUrl,
  type ChatRoomEvent,
  markAllRoomsRead,
  markRoomRead,
  sendChatMessage,
  setChatMessageStarred,
  updateChatRoom,
  promoteChatRoom,
  type ChatRoom,
  type ChatHistory,
  type ChatView,
} from '$lib/api';
import { loadSetting, saveSetting } from '$lib/stores/persisted';
import { resetCommandCatalogue } from '$lib/components/chat/autocomplete/providers';
import {
  applyEvent as applySegmentEvent,
  type ChatMessage,
  type Segment,
  type ToolEntry,
  type SearchResultsData,
  type SearchResultItem,
} from '$lib/stores/segments';

// The message / segment model lives in the pure reducer module so it can be
// unit-tested without a DOM; re-export here so existing `$lib/stores/chat`
// importers keep working.
export type { ChatMessage, Segment, ToolEntry, SearchResultsData, SearchResultItem };

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

const STREAM_KINDS = [
  'task_started',
  'tool_start',
  'tool_end',
  'tool_progress',
  'progress_text',
  'thinking',
  'text_delta',
  'context_management',
  'confirmation',
  'result',
  'error',
  'cancelled',
  'done',
];

export interface ChatSession {
  rooms: Writable<ChatRoom[]>;
  activeRoomId: Writable<number | null>;
  messages: Writable<ChatMessage[]>;
  status: Writable<ChatStatus>;
  activeTaskId: Writable<number | null>;
  loaded: Writable<boolean>;
  error: Writable<string>;
  // Cross-room aggregate views: 'room' renders the active room's live
  // transcript; the other three render a read-only stream across all member
  // rooms (no composer, no live streaming — reload on entry).
  view: Writable<'room' | ChatView>;
  selectView: (v: ChatView) => Promise<void>;
  // Star / unstar the durable message behind a transcript row (optimistic,
  // reverted on failure). No-op for rows without a msgId.
  toggleStar: (cid: number) => Promise<void>;
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
  send: (text: string, attachments?: { path: string; name: string }[]) => Promise<void>;
  cancel: () => Promise<void>;
  confirm: (cid: number, taskId: number) => Promise<void>;
  reject: (cid: number, taskId: number) => Promise<void>;
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
  const error = writable('');
  // Which pane the transcript renders: the active room, or a cross-room
  // aggregate view (All / Unread / Starred). Aggregate views are read-only
  // reading surfaces — no composer, no SSE; re-entering refreshes.
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
  // backfill) into the existing entries by id — no reorder, no drop of local
  // state. The active room is forced to 0 so looking at it always reads as
  // clear, even if a count lands before the mark-read round-trips.
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
        };
      });
      // Append rooms that newly surfaced (e.g. a Talk room first mirrored in).
      for (const fresh of list) {
        if (!seen.has(fresh.id)) merged.push({ ...fresh, unread_count: unreadFor(fresh) });
      }
      return merged;
    });
  }

  function startRoomsRefresh() {
    if (roomsTimer) return;
    roomsTimer = setInterval(() => {
      void refreshRooms();
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

  // Replay the frames held for the duration of a send, now that the turn's
  // task id is on screen and the ordinary dedup can recognise our own echo.
  function drainPendingSend() {
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
      // frames), so merge rather than replace.
      if (idx === -1) return [...rs, { ...(fresh as ChatRoom), unread_count: 0 }];
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
          target = (await getRoomEvents(roomCursor, 1, RECOVERY_FETCH_TIMEOUT_MS)).cursor;
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
        const page = await getRoomEvents(roomCursor);
        lastRoomEventAt = Date.now();
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
        es = new EventSource(chatRoomStreamUrl(roomCursor), { withCredentials: true });
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
    };
  }

  async function loadHistory(roomId: number, timeoutMs = 0) {
    const hist = await getRoomMessages(roomId, { timeoutMs });
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
    messages.set(msgs);
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
      messages.set(hist.messages.map(buildHistoryMessage));
      oldestCursor = hist.oldest_cursor ?? null;
      hasMore.set(!!hist.has_more);
    } catch {
      error.set('Failed to load messages');
    }
  }

  // Enter an aggregate view: tear down the room's live machinery (stream,
  // queue, notif poll, paging state), deselect the room, and load the first
  // page. The rooms-refresh timer keeps running so sidebar badges stay live.
  async function selectView(v: ChatView) {
    stopActive();
    view.set(v);
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
      error.set("Couldn't update star.");
    }
  }

  // Mark every room read in one shot (the header chip). Badges zero locally on
  // success; an open Unread view reloads to its (likely empty) fresh state.
  async function markAllRead() {
    try {
      await markAllRoomsRead();
    } catch {
      error.set("Couldn't mark all rooms read.");
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
      const { rooms: list } = await getChatRooms();
      if (superseded()) return;
      rooms.set(list);
      // Seed the stream cursor BEFORE the history read, not after. A row
      // committed in between is then re-delivered by the stream and dropped by
      // the `msg_id` dedup; seeding afterwards would place it below the cursor
      // *and* outside the rendered page — and `markRoomRead` below would have
      // already consumed it, so it would not even show as unread. Same
      // capture-before-reload discipline `recoverStream` uses.
      // (limit=1 → the server answers from its MAX(id) gate, not a serialized
      // page.)
      try {
        roomCursor = (await getRoomEvents(0, 1)).cursor;
      } catch {
        roomCursor = 0;
      }
      if (superseded()) return;
      const persisted = loadSetting<number | null>('chat.activeRoomId', null);
      const target = list.find((r) => r.id === persisted) ?? list[0];
      if (target) {
        activeRoomId.set(target.id);
        setRoomUnread(target.id, 0);
        await loadHistory(target.id);
        if (superseded()) return;
        markRoomRead(target.id).catch(() => {});
      }
      loaded.set(true);
      startRoomStream();
      // Slow metadata reconciler (see ROOMS_REFRESH_MS) — the stream is the
      // live path.
      startRoomsRefresh();
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
    } catch (e) {
      error.set('Failed to load chat');
    }
  }

  async function selectRoom(id: number) {
    if (get(activeRoomId) === id && get(view) === 'room') return;
    stopActive();
    view.set('room');
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
    rooms.update((r) => [...r, room]);
    await selectRoom(room.id);
  }

  async function renameRoom(id: number, name: string) {
    const updated = await updateChatRoom(id, { name });
    rooms.update((r) => r.map((x) => (x.id === id ? updated : x)));
  }

  async function updateRoomSettings(
    id: number,
    patch: { name?: string; model?: string | null; effort?: string | null },
  ) {
    const updated = await updateChatRoom(id, patch);
    rooms.update((r) => r.map((x) => (x.id === id ? updated : x)));
  }

  async function promoteRoom(id: number) {
    try {
      const updated = await promoteChatRoom(id);
      rooms.update((r) => r.map((x) => (x.id === id ? { ...x, ...updated } : x)));
    } catch {
      error.set("Couldn't open this room in Talk.");
    }
  }

  async function archiveRoom(id: number) {
    await updateChatRoom(id, { archived: true });
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
        error.set('This room has a task in progress — wait for it to finish or cancel it.');
      } else {
        error.set("Couldn't delete room.");
      }
      return;
    }
    // On success (or a 404 already-gone) drop it from the list, mirroring
    // archiveRoom's fall-through when the active room disappears.
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

  // Select the target room (if needed), locate the task's turn — paging older
  // history up to a bound when it's outside the loaded window — then scroll to
  // it. Returns false (and sets a transient error) on any miss rather than
  // throwing, so a stale/foreign link degrades gracefully.
  async function jumpToTask(roomToken: string, taskId: number): Promise<boolean> {
    try {
      const room = get(rooms).find((r) => r.token === roomToken);
      if (!room) {
        error.set("Couldn't open that conversation.");
        return false;
      }
      if (get(activeRoomId) !== room.id || get(view) !== 'room') {
        const ok = await selectRoomByToken(roomToken);
        if (!ok) {
          error.set("Couldn't open that conversation.");
          return false;
        }
      }
      let cid = findCidByTask(taskId);
      let pages = 0;
      while (cid == null && get(hasMore) && !get(loadingOlder) && pages < JUMP_MAX_PAGES) {
        await loadOlder();
        pages += 1;
        cid = findCidByTask(taskId);
      }
      if (cid == null) {
        error.set("Couldn't locate that message.");
        return false;
      }
      scrollToCid(cid);
      return true;
    } catch {
      error.set("Couldn't jump to that message.");
      return false;
    }
  }

  async function send(text: string, attachments: { path: string; name: string }[] = []) {
    const roomId = get(activeRoomId);
    const trimmed = text.trim();
    if (!roomId || (!trimmed && attachments.length === 0)) return;

    const userCid = nextCid();
    messages.update((a) => [
      ...a,
      {
        cid: userCid,
        role: 'user',
        text: trimmed,
        segments: [],
        streaming: false,
        attachments: attachments.map((x) => x.name),
        createdAt: new Date().toISOString(),
      },
    ]);
    const phCid = nextCid();
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
    status.set('sending');
    cancelRequested = false;

    // Hold this room's stream frames until the turn's task id is stamped
    // below — see `pendingSend`. A previous send's buffer can't still be open
    // (the composer blocks while sending), but drain defensively rather than
    // dropping rows if it somehow is.
    drainPendingSend();
    const sendToken = get(rooms).find((r) => r.id === roomId)?.token;
    if (sendToken) pendingSend = { token: sendToken, rows: [] };

    try {
      await sendTurn(roomId, trimmed, attachments, userCid, phCid);
    } finally {
      // Runs after the task id is on both halves of the turn, so the replayed
      // echo dedups instead of duplicating.
      drainPendingSend();
    }
  }

  async function sendTurn(
    roomId: number,
    trimmed: string,
    attachments: { path: string; name: string }[],
    userCid: number,
    phCid: number,
  ) {
    const res = await sendChatMessage(
      roomId,
      trimmed,
      attachments.map((x) => x.path),
    );
    if (!res.ok) {
      updateMsg(phCid, (m) => {
        const msg =
          res.status === 429
            ? `Rate limit reached — wait ${res.retry_after ?? 60}s and try again.`
            : res.error || 'Failed to send message.';
        m.text = msg;
        // Render the failure as the message's answer segment (the send
        // never reached the backend, so there's no event stream to build it).
        m.segments = [{ kind: 'text', id: 'send-error', text: msg, settled: false }];
        m.error = true;
        m.streaming = false;
        m.progress = undefined;
      });
      status.set('idle');
      return;
    }
    if (res.task_id == null) {
      // !command ran inline — no task, no stream.
      const cd = res.command_data as SearchResultsData | null | undefined;
      updateMsg(phCid, (m) => {
        m.role = 'system';
        m.text = res.inline_result || '';
        // A structured search_results payload renders as result cards; any
        // other kind (or absent data) falls back to the markdown text.
        if (cd && cd.kind === 'search_results') m.searchResults = cd;
        m.progress = undefined;
        m.streaming = false;
      });
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
    error,
    view,
    selectView,
    toggleStar,
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
    scrollToCid,
    scrollTarget,
    promoteRoom,
    archiveRoom,
    deleteRoom,
    send,
    cancel,
    confirm,
    reject,
    teardown,
  };
}

let _session: ChatSession | null = null;

export function getChatSession(): ChatSession {
  if (!_session) _session = createSession();
  return _session;
}
