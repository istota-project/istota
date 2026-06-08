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
	getRoomMessages,
	getChatRooms,
	getTaskEvents,
	sendChatMessage,
	updateChatRoom,
	type ChatRoom,
} from '$lib/api';
import { loadSetting, saveSetting } from '$lib/stores/persisted';

export interface ToolEntry {
	id: string;
	name: string;
	description: string;
	running: boolean;
	success?: boolean;
	// Live incremental output while the tool runs (NativeBrain tool_progress).
	progress?: string;
}

export interface ChatMessage {
	cid: number;
	role: 'user' | 'assistant' | 'system';
	text: string;
	taskId?: number;
	status?: string;
	confirmation?: boolean;
	tools: ToolEntry[];
	progress?: string;
	streaming: boolean;
	error?: boolean;
	attachments?: string[];
	createdAt?: string;
	// Total wall time in seconds, from the task's terminal `done` event.
	durationSeconds?: number;
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
	'text_delta', 'context_management', 'confirmation', 'result', 'error',
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
	init: () => Promise<void>;
	selectRoom: (id: number) => Promise<void>;
	selectRoomByToken: (token: string) => Promise<boolean>;
	newRoom: (name: string) => Promise<void>;
	renameRoom: (id: number, name: string) => Promise<void>;
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

	const updateMsg = (cid: number, fn: (m: ChatMessage) => void) => {
		messages.update((arr) => {
			const m = arr.find((x) => x.cid === cid);
			if (m) fn(m);
			return arr;
		});
	};

	// Mark every still-running tool as finished. Required because the Claude
	// Code brain never emits tool_end — without this, tool spinners would spin
	// forever once the task completes. success stays undefined (unknown) so the
	// UI shows a neutral "done" rather than a real success/fail mark.
	const finalizeTools = (m: ChatMessage) => {
		for (const t of m.tools) t.running = false;
	};

	function applyEvent(cid: number, kind: string, payload: Record<string, any>) {
		updateMsg(cid, (m) => {
			switch (kind) {
				case 'task_started':
					// Generic "working on it" verb stamped by the executor (shared
					// with Talk). We already seeded a client-side verb when the
					// placeholder was created, so skip the overwrite to avoid a
					// flicker from one random verb to another — real status
					// (progress_text / tool_start) still replaces it below.
					if (payload.text && !m.progress) m.progress = String(payload.text);
					break;
				case 'progress_text':
					m.progress = String(payload.text ?? '');
					break;
				case 'text_delta':
					// Incremental answer text streamed live (stream surfaces). Append
					// to the body; the canonical `result` replaces it on arrival
					// (the reconcile point). Clear the "thinking" verb — the growing
					// text is now the live signal (a typing affordance shows below it).
					m.text = (m.text ?? '') + String(payload.text ?? '');
					m.streaming = true;
					m.progress = undefined;
					break;
				case 'tool_start': {
					const name = String(payload.tool_name ?? 'tool');
					const description = String(payload.description ?? '');
					m.tools.push({
						id: String(payload.tool_call_id ?? `t${m.tools.length}`),
						name,
						description,
						running: true,
					});
					break;
				}
				case 'tool_progress': {
					// Incremental tool output (NativeBrain) — attach to the running
					// tool so the tool box shows live detail.
					const txt = String(payload.text ?? '');
					const t = m.tools.find((x) => x.id === String(payload.tool_call_id));
					if (t && txt) t.progress = txt;
					break;
				}
				case 'tool_end': {
					const t = m.tools.find((x) => x.id === String(payload.tool_call_id));
					if (t) {
						t.running = false;
						t.success = payload.success !== false;
					}
					break;
				}
				case 'result':
					m.text = String(payload.text ?? '');
					m.progress = undefined;
					m.streaming = false;
					finalizeTools(m);
					break;
				case 'confirmation':
					m.text = String(payload.prompt ?? '');
					m.confirmation = true;
					m.status = 'pending_confirmation';
					m.progress = undefined;
					m.streaming = false;
					finalizeTools(m);
					break;
				case 'error':
					m.text = String(payload.message ?? 'Something went wrong.');
					m.error = true;
					m.progress = undefined;
					m.streaming = false;
					finalizeTools(m);
					break;
				case 'cancelled':
					// A cancelled task has no canonical result to reconcile against.
					// If text streamed in via deltas (m.streaming still set), keep the
					// partial answer but mark it cancelled; otherwise show a bare notice.
					if (!m.text) m.text = '_(cancelled)_';
					else if (m.streaming) m.text = m.text + '\n\n_(cancelled)_';
					m.progress = undefined;
					m.streaming = false;
					finalizeTools(m);
					break;
				case 'done':
					// Terminal safety net: if no result/error/cancelled arrived,
					// still stop streaming and freeze any running tools.
					m.streaming = false;
					finalizeTools(m);
					if (typeof payload.duration_seconds === 'number') {
						m.durationSeconds = payload.duration_seconds;
					}
					break;
			}
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
			if (seq) lastSeq = Math.max(lastSeq, seq);
			let payload: Record<string, any> = {};
			try { payload = JSON.parse(dataStr); } catch { /* keep {} */ }
			applyEvent(cid, kind, payload);
			if (kind === 'confirmation') paused = true;
			if (kind === 'done' || kind === 'cancelled') settle();
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
	}

	// Halt the active stream and drop the queue without advancing — for room
	// switches and unmount. Remounting/reselecting resumes from history.
	function stopActive() {
		if (activeStream) { activeStream.stop(); activeStream = null; }
		streamQueue = [];
		stopNotifPolling();
		status.set('idle');
		activeTaskId.set(null);
	}

	function stopNotifPolling() {
		if (notifTimer) { clearInterval(notifTimer); notifTimer = null; }
	}

	// Poll the room's history while idle and surface any newly-delivered
	// bot messages (alerts / logs / web-routed notifications). Skipped while a
	// task streams — the stream owns the transcript then; the next idle tick
	// picks up anything that landed meanwhile.
	function startNotifPolling(roomId: number) {
		stopNotifPolling();
		notifTimer = setInterval(async () => {
			if (get(activeRoomId) !== roomId || activeStream || get(status) !== 'idle') return;
			let hist;
			try { hist = await getRoomMessages(roomId); } catch { return; }
			if (get(activeRoomId) !== roomId) return;
			for (const m of hist.messages) {
				if (m.role !== 'system' || typeof m.notif_id !== 'number') continue;
				if (seenNotifIds.has(m.notif_id)) continue;
				seenNotifIds.add(m.notif_id);
				messages.update((arr) => [...arr, {
					cid: nextCid(), role: 'system', text: m.text, tools: [],
					streaming: false, createdAt: m.created_at,
				}]);
			}
		}, NOTIF_POLL_MS);
	}

	async function loadHistory(roomId: number) {
		const hist = await getRoomMessages(roomId);
		// taskId → cid for assistant placeholders, so an in-flight task's stream
		// binds to the message the server already laid out in order.
		const cidByTask = new Map<number, number>();
		const inFlight = (s?: string) => s === 'pending' || s === 'locked' || s === 'running';
		// Reset the per-room dedup set, then record every notification already in
		// the transcript so the idle poller only appends ones that arrive later.
		seenNotifIds.clear();
		const msgs: ChatMessage[] = hist.messages.map((m) => {
			const cid = nextCid();
			if (m.role === 'assistant' && typeof m.task_id === 'number') {
				cidByTask.set(m.task_id, cid);
			}
			if (m.role === 'system' && typeof m.notif_id === 'number') {
				seenNotifIds.add(m.notif_id);
			}
			// Rebuild the action strip from the persisted trace so a finished
			// turn stays inspectable across reloads (ISSUE-122). Descriptions
			// only — history has no per-tool success/timing, so spinners stay
			// off and the strip renders a neutral "done" state.
			const tools: ToolEntry[] = (m.tools ?? []).map((d, i) => ({
				id: `h${i}`, name: '', description: d, running: false,
			}));
			return {
				cid,
				role: m.role,
				text: m.text,
				taskId: m.task_id,
				status: m.status,
				confirmation: !!m.confirmation,
				tools,
				streaming: m.role === 'assistant' && inFlight(m.status),
				createdAt: m.created_at,
				durationSeconds: typeof m.duration_seconds === 'number' ? m.duration_seconds : undefined,
			};
		});
		messages.set(msgs);
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
					status: at.status, tools: [], streaming: true,
					createdAt: new Date().toISOString(),
				};
				messages.update((arr) => { arr.push(ph); return arr; });
				cid = ph.cid;
			}
			enqueueStream(at.id, cid);
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
				await loadHistory(target.id);
			}
			loaded.set(true);
		} catch (e) {
			error.set('Failed to load chat');
		}
	}

	async function selectRoom(id: number) {
		if (get(activeRoomId) === id) return;
		stopActive();
		activeRoomId.set(id);
		saveSetting('chat.activeRoomId', id);
		messages.set([]);
		await loadHistory(id);
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
				cid: nextCid(), role: 'user', text: trimmed, tools: [], streaming: false,
				attachments: attachments.map((x) => x.name),
				createdAt: new Date().toISOString(),
			},
		]);
		const phCid = nextCid();
		messages.update((a) => [
			...a,
			{
				cid: phCid, role: 'assistant', text: '', tools: [], streaming: true,
				progress: randomAckVerb(),
				createdAt: new Date().toISOString(),
			},
		]);
		status.set('sending');

		const res = await sendChatMessage(roomId, trimmed, attachments.map((x) => x.path));
		if (!res.ok) {
			updateMsg(phCid, (m) => {
				m.text = res.status === 429
					? `Rate limit reached — wait ${res.retry_after ?? 60}s and try again.`
					: (res.error || 'Failed to send message.');
				m.error = true;
				m.streaming = false;
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
			m.text = m.text ? `~~${m.text}~~` : '_(declined)_';
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
	}

	return {
		rooms, activeRoomId, messages, status, activeTaskId, loaded, error,
		init, selectRoom, selectRoomByToken, newRoom, renameRoom, archiveRoom,
		deleteRoom, send, cancel, confirm, reject, teardown,
	};
}

let _session: ChatSession | null = null;

export function getChatSession(): ChatSession {
	if (!_session) _session = createSession();
	return _session;
}
