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
}

export type ChatStatus = 'idle' | 'sending' | 'streaming';

const STREAM_KINDS = [
	'task_started', 'tool_start', 'tool_end', 'tool_progress', 'progress_text',
	'context_management', 'confirmation', 'result', 'error', 'cancelled', 'done',
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
	newRoom: (name: string) => Promise<void>;
	renameRoom: (id: number, name: string) => Promise<void>;
	archiveRoom: (id: number) => Promise<void>;
	send: (text: string, attachments?: { path: string; name: string }[]) => Promise<void>;
	cancel: () => Promise<void>;
	confirm: (cid: number, taskId: number) => Promise<void>;
	reject: (cid: number, taskId: number) => Promise<void>;
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
	let activeStream: { stop: () => void } | null = null;

	const updateMsg = (cid: number, fn: (m: ChatMessage) => void) => {
		messages.update((arr) => {
			const m = arr.find((x) => x.cid === cid);
			if (m) fn(m);
			return arr;
		});
	};

	function applyEvent(cid: number, kind: string, payload: Record<string, any>) {
		updateMsg(cid, (m) => {
			switch (kind) {
				case 'progress_text':
					m.progress = String(payload.text ?? '');
					break;
				case 'tool_start':
					m.tools.push({
						id: String(payload.tool_call_id ?? `t${m.tools.length}`),
						name: String(payload.tool_name ?? 'tool'),
						description: String(payload.description ?? ''),
						running: true,
					});
					break;
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
					break;
				case 'confirmation':
					m.text = String(payload.prompt ?? '');
					m.confirmation = true;
					m.status = 'pending_confirmation';
					m.progress = undefined;
					m.streaming = false;
					break;
				case 'error':
					m.text = String(payload.message ?? 'Something went wrong.');
					m.error = true;
					m.progress = undefined;
					m.streaming = false;
					break;
				case 'cancelled':
					if (!m.text) m.text = '_(cancelled)_';
					m.progress = undefined;
					m.streaming = false;
					break;
			}
		});
	}

	function streamTask(taskId: number, cid: number): { stop: () => void } {
		let lastSeq = 0;
		let es: EventSource | null = null;
		let pollTimer: ReturnType<typeof setInterval> | null = null;
		let finished = false;

		const finish = () => {
			if (finished) return;
			finished = true;
			if (es) { es.close(); es = null; }
			if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
			status.set('idle');
			activeTaskId.set(null);
		};

		const handle = (kind: string, dataStr: string, seq: number) => {
			if (seq) lastSeq = Math.max(lastSeq, seq);
			let payload: Record<string, any> = {};
			try { payload = JSON.parse(dataStr); } catch { /* keep {} */ }
			applyEvent(cid, kind, payload);
			if (kind === 'done' || kind === 'cancelled') finish();
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
				es.addEventListener(k, (e: MessageEvent) =>
					handle(k, e.data, Number(e.lastEventId) || 0),
				);
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

		return { stop: finish };
	}

	function beginStream(taskId: number, cid: number) {
		if (activeStream) activeStream.stop();
		status.set('streaming');
		activeTaskId.set(taskId);
		activeStream = streamTask(taskId, cid);
	}

	async function loadHistory(roomId: number) {
		const hist = await getRoomMessages(roomId);
		const msgs: ChatMessage[] = hist.messages.map((m) => ({
			cid: nextCid(),
			role: m.role,
			text: m.text,
			taskId: m.task_id,
			status: m.status,
			confirmation: !!m.confirmation,
			tools: [],
			streaming: false,
		}));
		messages.set(msgs);

		if (hist.active_task) {
			const at = hist.active_task;
			let cid = 0;
			messages.update((arr) => {
				const existing = [...arr].reverse().find(
					(x) => x.role === 'assistant' && x.taskId === at.id,
				);
				if (existing) {
					cid = existing.cid;
					if (at.status !== 'pending_confirmation') existing.streaming = true;
				} else {
					const ph: ChatMessage = {
						cid: nextCid(), role: 'assistant', text: '', taskId: at.id,
						status: at.status, tools: [], streaming: true,
					};
					arr.push(ph);
					cid = ph.cid;
				}
				return arr;
			});
			if (at.status !== 'pending_confirmation') beginStream(at.id, cid);
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
		if (activeStream) { activeStream.stop(); activeStream = null; }
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
			else { activeRoomId.set(null); messages.set([]); }
		}
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
			},
		]);
		const phCid = nextCid();
		messages.update((a) => [
			...a,
			{ cid: phCid, role: 'assistant', text: '', tools: [], streaming: true },
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
				m.streaming = false;
			});
			status.set('idle');
			return;
		}
		updateMsg(phCid, (m) => { m.taskId = res.task_id!; m.status = 'pending'; });
		beginStream(res.task_id, phCid);
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
		beginStream(taskId, cid);
	}

	async function reject(cid: number, taskId: number) {
		try { await cancelChatTask(taskId); } catch { /* ignore */ }
		updateMsg(cid, (m) => {
			m.confirmation = false;
			m.status = 'cancelled';
			m.streaming = false;
			m.text = m.text ? `~~${m.text}~~` : '_(declined)_';
		});
		status.set('idle');
		activeTaskId.set(null);
	}

	return {
		rooms, activeRoomId, messages, status, activeTaskId, loaded, error,
		init, selectRoom, newRoom, renameRoom, archiveRoom,
		send, cancel, confirm, reject,
	};
}

let _session: ChatSession | null = null;

export function getChatSession(): ChatSession {
	if (!_session) _session = createSession();
	return _session;
}
