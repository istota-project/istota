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
	getTaskEvents,
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
import { applyEvent as applySegmentEvent, type ChatMessage, type Segment, type ToolEntry } from '$lib/stores/segments';

// The message / segment model lives in the pure reducer module so it can be
// unit-tested without a DOM; re-export here so existing `$lib/stores/chat`
// importers keep working.
export type { ChatMessage, Segment, ToolEntry };

/** Build an assistant message's `segments` from a finished task's history
 * payload. Tool entries render as neutral "done" chips (history carries no
 * per-tool success / progress / timing); the last text segment is the answer
 * (unsettled, prominent), all earlier text segments are settled narration. */
function historySegments(raw: { kind: string; text: string }[]): Segment[] {
	const segs: Segment[] = raw.map((s, i) => {
		if (s.kind === 'tool') {
			return { kind: 'tool', id: `h${i}`, tool: { id: `h${i}`, name: '', description: s.text, running: false } };
		}
		if (s.kind === 'thinking') {
			return { kind: 'thinking', id: `k${i}`, text: s.text, settled: true };
		}
		return { kind: 'text', id: `s${i}`, text: s.text, settled: true };
	});
	// Only the last *text* segment is the answer; thinking stays settled.
	for (let i = segs.length - 1; i >= 0; i--) {
		const s = segs[i];
		if (s.kind === 'text') { s.settled = false; break; }
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
	'On it…', 'Hmm…', 'Heard, chef…', 'Investigating…', 'One sec…',
	'Copy that…', 'Roger…', 'Considering…', 'Thinkifying…', 'Braining…',
	'Improvising…', 'Jamming…', 'Riffing…', 'Grooving…', 'Beboppin’…',
	'Noodling…', 'Syncopating…', 'Comping…', 'Soloing…',
	// Cephalopod
	'Inking…', 'Tentacling…', 'Suckering…', 'Jetting…', 'Unfurling…',
	'Chromatophoring…', 'Squidding…', 'Grasping…', 'Probing…', 'Siphoning…',
	// Cheeky
	'Instigating…', 'Scheming…', 'Concocting…', 'Percolating…', 'Marinating…',
	'Hatching…', 'Sleuthing…', 'Finagling…', 'Wrangling…', 'Tinkering…',
	'Rummaging…', 'Conjuring…', 'Fermenting…', 'Machinating…', 'Gallivanting…',
];

function randomAckVerb(): string {
	return ACK_VERBS[Math.floor(Math.random() * ACK_VERBS.length)];
}

const STREAM_KINDS = [
	'task_started', 'tool_start', 'tool_end', 'tool_progress', 'progress_text',
	'thinking', 'text_delta', 'context_management', 'confirmation', 'result', 'error',
	'cancelled', 'done',
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
	// surface) are appended to the room out-of-band — they have no task to
	// stream. When the room is idle we poll its history and surface any new ones.
	// `seenNotifIds` dedups across polls; it's reset per room in loadHistory.
	const seenNotifIds = new Set<number>();
	let notifTimer: ReturnType<typeof setInterval> | null = null;
	const NOTIF_POLL_MS = 5000;
	// Sidebar unread badges. A separate timer re-fetches the room list (counts
	// included) so a notification / scheduled post / mirrored Talk turn landing
	// in a *non-active* room lights it up without a reload. Runs regardless of
	// which room is active (unlike the notif poll, which is scoped to the open
	// room) but is cheap — one rooms-list call.
	let roomsTimer: ReturnType<typeof setInterval> | null = null;
	const ROOMS_REFRESH_MS = 5000;
	let onVisibility: (() => void) | null = null;

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
			if (es) { es.close(); es = null; }
			if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
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
			try { payload = JSON.parse(dataStr); } catch { /* keep {} */ }
			// A reducer/render throw must never wedge the stream — keep advancing
			// so later events (notably `result` / `done`) still apply.
			try { applyEvent(cid, kind, payload); } catch { /* swallow */ }
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
			} catch { /* transient; try again next tick */ }
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
				if (es) { es.close(); es = null; }
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
			updateMsg(cid, (m) => { if (!m.progress) m.progress = 'Queued…'; });
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
			if (next) { startStream(next.taskId, next.cid); return; }
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
		if (activeStream) { activeStream.stop(); activeStream = null; }
		streamQueue = [];
		stopNotifPolling();
		resetPaging();
		status.set('idle');
		activeTaskId.set(null);
	}

	function stopNotifPolling() {
		if (notifTimer) { clearInterval(notifTimer); notifTimer = null; }
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
		markRoomRead(roomId).catch(() => { /* transient; next open/poll retries */ });
	}

	// Re-fetch the room list and merge fresh unread counts (and any name/origin
	// backfill) into the existing entries by id — no reorder, no drop of local
	// state. The active room is forced to 0 so looking at it always reads as
	// clear, even if a count lands before the mark-read round-trips.
	async function refreshRooms() {
		let list: ChatRoom[];
		try { ({ rooms: list } = await getChatRooms()); } catch { return; }
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
		roomsTimer = setInterval(() => { void refreshRooms(); }, ROOMS_REFRESH_MS);
	}

	function stopRoomsRefresh() {
		if (roomsTimer) { clearInterval(roomsTimer); roomsTimer = null; }
	}

	// Poll the room's history while idle and surface (a) newly-delivered bot
	// messages (alerts / logs / web-routed notifications) and (b) a task that
	// *started* while this room was open — most importantly a Talk-originated
	// turn (unified room sync): its user message is shown and its progress
	// streamed live, so the conversation animates in both surfaces at once.
	// Skipped while a task streams — the stream owns the transcript then; the
	// next idle tick picks up anything that landed meanwhile.
	function startNotifPolling(roomId: number) {
		stopNotifPolling();
		notifTimer = setInterval(async () => {
			if (get(activeRoomId) !== roomId || activeStream || get(status) !== 'idle') return;
			let hist;
			try { hist = await getRoomMessages(roomId); } catch { return; }
			if (get(activeRoomId) !== roomId) return;
			let appended = false;
			for (const m of hist.messages) {
				if (m.role !== 'system' || typeof m.notif_id !== 'number') continue;
				if (seenNotifIds.has(m.notif_id)) continue;
				seenNotifIds.add(m.notif_id);
				appended = true;
				messages.update((arr) => [...arr, {
					cid: nextCid(), role: 'system', text: m.text, segments: [],
					streaming: false, createdAt: m.created_at,
					// Carry the durable star key from the same history row (ISSUE-172):
					// a live-appended notification is starrable immediately, not only
					// after a reload re-reads it through buildHistoryMessage.
					msgId: typeof m.msg_id === 'number' ? m.msg_id : undefined,
					starred: typeof m.msg_id === 'number' ? !!m.starred : undefined,
				}]);
			}
			// A notification just landed in the open room — persist the read cursor
			// past it (visibility-gated) so it doesn't resurface as unread later.
			if (appended) markActiveRead(roomId);
			pickUpNewInFlightTasks(hist);
		}, NOTIF_POLL_MS);
	}

	// Surface in-flight tasks not yet in the transcript (e.g. a Talk turn that
	// started while the web room was open) and stream them live. Cross-surface
	// progress: the same SSE substrate the web client already tails works for a
	// Talk-source task because the events endpoint is ownership-gated, not
	// source-gated. A fast turn that already completed between polls is picked up
	// on the next room load — this path is the live, in-flight case.
	function pickUpNewInFlightTasks(hist: ChatHistory) {
		const actives = hist.active_tasks ?? (hist.active_task ? [hist.active_task] : []);
		if (!actives.length) return;
		const known = new Set<number>();
		for (const m of get(messages)) if (typeof m.taskId === 'number') known.add(m.taskId);
		const cur = get(activeTaskId);
		if (cur != null) known.add(cur);
		for (const q of streamQueue) known.add(q.taskId);
		for (const at of actives) {
			if (at.status === 'pending_confirmation' || known.has(at.id)) continue;
			// Show the turn's user message first, if the room history carries it.
			const um = hist.messages.find((m) => m.role === 'user' && m.task_id === at.id);
			if (um) {
				messages.update((arr) => [...arr, {
					cid: nextCid(), role: 'user', text: um.text, taskId: at.id,
					segments: [], streaming: false, createdAt: um.created_at,
					// Star key from the history row, parity with buildHistoryMessage
					// (ISSUE-172) so a picked-up user turn is starrable without reload.
					msgId: typeof um.msg_id === 'number' ? um.msg_id : undefined,
					starred: typeof um.msg_id === 'number' ? !!um.starred : undefined,
				}]);
			}
			const ph: ChatMessage = {
				cid: nextCid(), role: 'assistant', text: '', taskId: at.id,
				status: at.status, segments: [], streaming: true,
				createdAt: new Date().toISOString(),
			};
			messages.update((arr) => [...arr, ph]);
			enqueueStream(at.id, ph.cid);
		}
	}

	const inFlight = (s?: string) => s === 'pending' || s === 'locked' || s === 'running';

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

	async function loadHistory(roomId: number) {
		const hist = await getRoomMessages(roomId);
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
		startNotifPolling(roomId);

		// Resume the room's in-flight tasks in order: the first streams, the rest
		// queue behind it. A leading pending_confirmation is left parked (its card
		// is shown) — the user must act before the queue moves.
		const actives = hist.active_tasks ?? (hist.active_task ? [hist.active_task] : []);
		for (const at of actives) {
			if (at.status === 'pending_confirmation') continue;
			let cid = cidByTask.get(at.id);
			if (cid == null) {
				const ph: ChatMessage = {
					cid: nextCid(), role: 'assistant', text: '', taskId: at.id,
					status: at.status, segments: [], streaming: true,
					createdAt: new Date().toISOString(),
				};
				messages.update((arr) => { arr.push(ph); return arr; });
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
		updateMsg(cid, (mm) => { mm.starred = next; });
		try {
			await setChatMessageStarred(m.msgId, next);
			if (!next && get(view) === 'starred') {
				messages.update((arr) => arr.filter((x) => x.cid !== cid));
			}
		} catch {
			updateMsg(cid, (mm) => { mm.starred = !next; });
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
		try {
			const cfg = await getChatConfig().catch(() => null);
			if (cfg?.client_poll_interval_ms) pollIntervalMs = cfg.client_poll_interval_ms;
			const { rooms: list } = await getChatRooms();
			rooms.set(list);
			const persisted = loadSetting<number | null>('chat.activeRoomId', null);
			const target = list.find((r) => r.id === persisted) ?? list[0];
			if (target) {
				activeRoomId.set(target.id);
				setRoomUnread(target.id, 0);
				await loadHistory(target.id);
				markRoomRead(target.id).catch(() => {});
			}
			loaded.set(true);
			// Keep sidebar badges live, and clear the open room when the tab regains
			// focus (messages that arrived while backgrounded were held unread).
			startRoomsRefresh();
			if (typeof document !== 'undefined') {
				onVisibility = () => {
					if (document.visibilityState !== 'visible') return;
					const rid = get(activeRoomId);
					if (rid != null) markActiveRead(rid);
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
		markRoomRead(id).catch(() => { /* non-fatal; refresh/poll will retry */ });
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
			else { stopNotifPolling(); activeRoomId.set(null); messages.set([]); }
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
			else { stopNotifPolling(); activeRoomId.set(null); messages.set([]); }
		}
	}

	async function selectRoomByToken(token: string): Promise<boolean> {
		const room = get(rooms).find((r) => r.token === token);
		if (!room) return false;
		await selectRoom(room.id);
		return true;
	}

	async function send(text: string, attachments: { path: string; name: string }[] = []) {
		const roomId = get(activeRoomId);
		const trimmed = text.trim();
		if (!roomId || (!trimmed && attachments.length === 0)) return;

		messages.update((a) => [
			...a,
			{
				cid: nextCid(), role: 'user', text: trimmed, segments: [], streaming: false,
				attachments: attachments.map((x) => x.name),
				createdAt: new Date().toISOString(),
			},
		]);
		const phCid = nextCid();
		messages.update((a) => [
			...a,
			{
				cid: phCid, role: 'assistant', text: '', segments: [], streaming: true,
				progress: randomAckVerb(),
				createdAt: new Date().toISOString(),
			},
		]);
		status.set('sending');

		const res = await sendChatMessage(roomId, trimmed, attachments.map((x) => x.path));
		if (!res.ok) {
			updateMsg(phCid, (m) => {
				const msg = res.status === 429
					? `Rate limit reached — wait ${res.retry_after ?? 60}s and try again.`
					: (res.error || 'Failed to send message.');
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
			updateMsg(phCid, (m) => {
				m.role = 'system';
				m.text = res.inline_result || '';
				m.progress = undefined;
				m.streaming = false;
			});
			status.set('idle');
			return;
		}
		updateMsg(phCid, (m) => { m.taskId = res.task_id!; m.status = 'pending'; });
		// Stream now if the room is free, otherwise queue behind the in-flight
		// task. The backend gate keeps this task pending until its turn either way.
		enqueueStream(res.task_id, phCid);
	}

	async function cancel() {
		const taskId = get(activeTaskId);
		if (taskId == null) return;
		try { await cancelChatTask(taskId); } catch { /* ignore */ }
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
		try { await cancelChatTask(taskId); } catch { /* ignore */ }
		updateMsg(cid, (m) => {
			m.confirmation = false;
			m.status = 'cancelled';
			m.streaming = false;
			// Strike the declined prompt (the trailing text segment), or leave a
			// bare notice when there was none.
			const last = m.segments[m.segments.length - 1];
			if (last && last.kind === 'text' && last.text) last.text = `~~${last.text}~~`;
			else m.segments.push({ kind: 'text', id: 'declined', text: '_(declined)_', settled: false });
			m.text = m.segments[m.segments.length - 1].kind === 'text'
				? (m.segments[m.segments.length - 1] as Extract<Segment, { kind: 'text' }>).text : '';
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
		stopActive();
		stopRoomsRefresh();
		// Drop the cached command/alias catalogue so a fresh session refetches it.
		resetCommandCatalogue();
		if (onVisibility && typeof document !== 'undefined') {
			document.removeEventListener('visibilitychange', onVisibility);
			onVisibility = null;
		}
	}

	return {
		rooms, activeRoomId, messages, status, activeTaskId, loaded, error,
		view, selectView, toggleStar, markAllRead,
		hasMore, loadingOlder, loadOlder,
		init, selectRoom, selectRoomByToken, newRoom, renameRoom, updateRoomSettings,
		promoteRoom, archiveRoom,
		deleteRoom, send, cancel, confirm, reject, teardown,
	};
}

let _session: ChatSession | null = null;

export function getChatSession(): ChatSession {
	if (!_session) _session = createSession();
	return _session;
}
