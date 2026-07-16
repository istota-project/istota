/**
 * Pure event → segment reducer for assistant chat messages.
 *
 * An assistant turn is an *ordered list of segments* — `text` and `tool` —
 * built from the `task_events` stream in `seq` order, mirroring the model's
 * true block order. This dissolves the narration-vs-answer ambiguity: we don't
 * guess a text block's role at token-arrival time, we render it live and let
 * the *next* event settle it. A `tool_start` settles the open text block (it
 * was narration); a terminal event leaves the last text block as the answer.
 *
 * This module is intentionally pure — no Svelte / store imports — so the
 * reducer is unit-testable under vitest without a DOM. `chat.ts` re-exports the
 * types and calls `applyEvent` inside its `updateMsg` mutation.
 */

export interface ToolEntry {
	id: string; // tool_call_id (or synthesized t<n> / h<n>)
	name: string;
	description: string; // model's own action description
	running: boolean;
	success?: boolean;
	// Live incremental output WHILE running (NativeBrain tool_progress);
	// cleared on tool_end.
	progress?: string;
}

export type Segment =
	| { kind: 'text'; id: string; text: string; settled: boolean }
	| { kind: 'thinking'; id: string; text: string; settled: boolean }
	| { kind: 'tool'; id: string; tool: ToolEntry };

export interface ChatMessage {
	cid: number;
	role: 'user' | 'assistant' | 'system';
	// User/system body; for an assistant turn this mirrors the canonical answer
	// (the last text segment) for copy-to-clipboard / aria / persistence. The
	// rendered assistant body comes from `segments`, not this field.
	text: string;
	// Assistant only; [] for user/system.
	segments: Segment[];
	taskId?: number;
	status?: string;
	confirmation?: boolean;
	error?: boolean;
	streaming: boolean;
	// Ack verb shown before the first segment exists.
	progress?: string;
	attachments?: string[];
	createdAt?: string;
	// Total wall time in seconds, from the task's terminal `done` event.
	durationSeconds?: number;
	// The model that produced this answer (canonical ID), from the terminal
	// `done` event or the history payload. Shown in the message meta.
	model?: string;
	// Durable-store identity: the `messages.id` star key. Absent for in-flight /
	// failed turns that exist only as tasks rows (not starrable) and for locally
	// appended placeholders.
	msgId?: number;
	// Whether the current user has starred this message.
	starred?: boolean;
	// Set on aggregate-view (All / Unread / Starred) rows so the transcript can
	// label each message with its room and jump to it.
	roomToken?: string;
	roomName?: string;
}

// ---- Helpers ----------------------------------------------------------------

let _textSegSeq = 0;
function nextTextId(): string {
	return `s${++_textSegSeq}`;
}

let _thinkSegSeq = 0;
function nextThinkId(): string {
	return `k${++_thinkSegSeq}`;
}

/** The last segment if it is an open (unsettled) text segment; otherwise push a
 * fresh open text segment and return it. Only called from the `text_delta`
 * branch, so a tool-first turn never gets an empty leading text segment. */
export function openTextSegment(m: ChatMessage): Extract<Segment, { kind: 'text' }> {
	const last = m.segments[m.segments.length - 1];
	if (last && last.kind === 'text' && !last.settled) return last;
	const seg = { kind: 'text' as const, id: nextTextId(), text: '', settled: false };
	m.segments.push(seg);
	return seg;
}

/** The last segment if it is an open (unsettled) thinking segment; otherwise
 * push a fresh open thinking segment and return it. Mirrors openTextSegment —
 * only called from the `thinking` branch, so a turn with no thinking never gets
 * an empty leading thinking segment. */
export function openThinkingSegment(m: ChatMessage): Extract<Segment, { kind: 'thinking' }> {
	const last = m.segments[m.segments.length - 1];
	if (last && last.kind === 'thinking' && !last.settled) return last;
	const seg = { kind: 'thinking' as const, id: nextThinkId(), text: '', settled: false };
	m.segments.push(seg);
	return seg;
}

/** Settle the open trailing block — text OR thinking — if any. "Something came
 * after this block (a tool, or the answer), so it was lead-in, not the answer."
 * A no-op when the last segment isn't an open text/thinking block. */
export function settleOpenBlock(m: ChatMessage): void {
	const last = m.segments[m.segments.length - 1];
	if (last && (last.kind === 'text' || last.kind === 'thinking') && !last.settled) {
		last.settled = true;
	}
}

/** Settle the open trailing block only when it is of `kind`. Used at the
 * thinking↔answer boundary, where a thinking segment must settle before answer
 * text opens (and vice-versa) without disturbing an open block of the other
 * kind. */
function settleOpenOfKind(m: ChatMessage, kind: 'text' | 'thinking'): void {
	const last = m.segments[m.segments.length - 1];
	if (last && last.kind === kind && !last.settled) last.settled = true;
}

export function findTool(m: ChatMessage, id: string): Extract<Segment, { kind: 'tool' }> | undefined {
	for (const s of m.segments) {
		if (s.kind === 'tool' && s.tool.id === id) return s;
	}
	return undefined;
}

/** Text of the last `text` segment, or '' when there is none. This is the
 * answer once the message is terminal. */
export function answerText(m: ChatMessage): string {
	for (let i = m.segments.length - 1; i >= 0; i--) {
		const s = m.segments[i];
		if (s.kind === 'text') return s.text;
	}
	return '';
}

/** Set the trailing answer/error/prompt text: overwrite the last segment if it
 * is a text segment, else append a fresh (unsettled) text segment. A settled
 * text segment is never the last segment (settling only happens alongside a
 * tool push), so a trailing text segment is always the open answer slot. */
function setTrailingText(m: ChatMessage, text: string): void {
	const last = m.segments[m.segments.length - 1];
	if (last && last.kind === 'text') {
		last.text = text;
		last.settled = false;
	} else {
		m.segments.push({ kind: 'text', id: nextTextId(), text, settled: false });
	}
}

/** Mark every still-running tool finished. The Claude Code brain never emits
 * tool_end, so without this a tool chip would spin forever once the task
 * completes. `success` stays as-is (undefined → neutral "done"). */
export function finalizeTools(m: ChatMessage): void {
	for (const s of m.segments) {
		if (s.kind === 'tool') s.tool.running = false;
	}
}

/** Whether a segment should render. A settled text segment whose trimmed text
 * is empty is suppressed (no empty collapsed narration row). */
export function isRenderable(seg: Segment): boolean {
	if (seg.kind === 'tool') return true;
	if (seg.settled && seg.text.trim() === '') return false;
	return true;
}

// ---- Body layout (render groups) --------------------------------------------

/** A non-trailing text block is kept in the rendered body iff its trimmed length
 * crosses this bar — i.e. it is substantive content the model wrote and then
 * acted on (an analysis before an edit), not throwaway lead-in narration ("Let
 * me check…"). Mirrors the backend's `stream_text_gate_chars` (default 280): on
 * a stream surface the executor only ever *streams* a text run once it crosses
 * that gate, so a sub-threshold intermediate block can only arrive via the
 * history (`execution_trace`) path — and the same bar drops it there, keeping
 * the live and reloaded layouts identical. The trailing answer is exempt: it
 * always renders, however short.
 *
 * MUST stay equal to the backend `scheduler.stream_text_gate_chars` default
 * (`config.py`, 280). They are independent constants; if that knob is tuned away
 * from 280 in production, this value has to move with it, or the live stream
 * (gated server-side) and a reloaded-from-trace turn (gated here) would classify
 * a borderline block differently. */
export const SUBSTANTIAL_TEXT_CHARS = 280;

/** One renderable unit of an assistant turn's body, in true segment order. */
export type RenderGroup =
	| { kind: 'prose'; id: string; text: string }
	| { kind: 'activity'; id: string; steps: Segment[] };

/** Reduce an assistant turn's ordered segments into the body's render groups —
 * substantial prose blocks and activity chips, interleaved in the model's true
 * block order.
 *
 * A `text` segment renders as a `prose` group iff it is substantial (trimmed
 * length ≥ `threshold`) OR it is the final text segment (the canonical answer
 * always renders). Shorter intermediate text — lead-in narration — is dropped.
 * `thinking` never reaches the body (it folds into the activity cue). Runs of
 * consecutive `tool` segments (including any short narration skipped between
 * them) coalesce into one `activity` group, so the chip count matches the
 * model's actual work phases.
 *
 * Pure — same input → same output — so it drives both the live stream and a
 * reloaded-from-history turn identically. */
export function renderGroups(m: ChatMessage, threshold = SUBSTANTIAL_TEXT_CHARS): RenderGroup[] {
	let lastTextIdx = -1;
	for (let i = m.segments.length - 1; i >= 0; i--) {
		if (m.segments[i].kind === 'text') {
			lastTextIdx = i;
			break;
		}
	}
	const groups: RenderGroup[] = [];
	let toolRun: Segment[] = [];
	const flushTools = (): void => {
		if (toolRun.length) {
			groups.push({ kind: 'activity', id: `act-${toolRun[0].id}`, steps: toolRun });
			toolRun = [];
		}
	};
	m.segments.forEach((s, i) => {
		if (s.kind === 'tool') {
			toolRun.push(s);
			return;
		}
		if (s.kind === 'thinking') return; // reasoning never renders in the body
		// A whitespace-only block never renders (mirrors isRenderable's
		// empty-settled suppression) — including an empty trailing answer, which
		// would otherwise emit a blank `.body` div.
		const trimmedLen = s.text.trim().length;
		if (trimmedLen === 0) return;
		const substantial = trimmedLen >= threshold;
		if (i === lastTextIdx || substantial) {
			flushTools();
			groups.push({ kind: 'prose', id: s.id, text: s.text });
		}
		// else: short intermediate narration — drop it, letting the tool run on
		// either side coalesce into one chip.
	});
	flushTools();
	return groups;
}

// ---- Reducer ----------------------------------------------------------------

/** Apply one `task_event` to an assistant message, mutating it in place.
 *
 * `task_started` is NOT handled here — its ack-verb seeding lives in chat.ts
 * (it's message state, not a segment, and competes with the client-side seed).
 * Unknown kinds are ignored. Missing payload fields coerce to defaults. */
export function applyEvent(m: ChatMessage, kind: string, payload: Record<string, unknown>): void {
	switch (kind) {
		case 'progress_text':
			m.progress = String(payload.text ?? '');
			break;

		case 'thinking': {
			// Real extended-thinking / reasoning from the brain. Accumulates into a
			// distinct thinking segment that renders in the activity chip — never the
			// answer. A late stray delta after the message terminated is ignored.
			if (!m.streaming) break;
			// thinking after answer text shouldn't reopen the answer block; settle
			// an open text block at the answer→thinking boundary.
			settleOpenOfKind(m, 'text');
			const seg = openThinkingSegment(m);
			seg.text += String(payload.text ?? '');
			m.progress = undefined;
			break;
		}

		case 'text_delta': {
			// A late stray delta after the message terminated must not reopen a
			// finished answer.
			if (!m.streaming) break;
			// Settle an open thinking block first (thinking → answer boundary) so the
			// reasoning lead-in folds into the chip and the answer opens fresh.
			settleOpenOfKind(m, 'thinking');
			const seg = openTextSegment(m);
			seg.text += String(payload.text ?? '');
			m.progress = undefined;
			break;
		}

		case 'tool_start': {
			// The text/thinking streamed so far was this turn's lead-in (a tool
			// follows it) — settle it so it folds to a collapsed disclosure.
			settleOpenBlock(m);
			const toolCount = m.segments.filter((s) => s.kind === 'tool').length;
			// ClaudeCodeBrain (the default brain) emits an EMPTY tool_call_id, so a
			// `?? fallback` (null/undefined only) would key every tool in a
			// multi-tool turn to "" — duplicate keys in the `{#each}`. Treat an
			// empty/non-string id as missing and synthesize a positional one.
			const raw = payload.tool_call_id;
			const id = typeof raw === 'string' && raw ? raw : `t${toolCount}`;
			m.segments.push({
				kind: 'tool',
				id,
				tool: {
					id,
					name: String(payload.tool_name ?? 'tool'),
					description: String(payload.description ?? ''),
					running: true,
				},
			});
			break;
		}

		case 'tool_progress': {
			const txt = String(payload.text ?? '');
			const t = findTool(m, String(payload.tool_call_id));
			if (t && txt) t.tool.progress = txt;
			break;
		}

		case 'tool_end': {
			const t = findTool(m, String(payload.tool_call_id));
			if (t) {
				t.tool.running = false;
				t.tool.success = payload.success !== false;
				t.tool.progress = undefined;
			}
			break;
		}

		case 'result': {
			// Reconcile the canonical (CM-composed) answer. Only overwrite when
			// non-empty: an empty result keeps whatever streamed in as the answer.
			const text = String(payload.text ?? '');
			if (text) setTrailingText(m, text);
			m.text = answerText(m);
			m.progress = undefined;
			m.streaming = false;
			finalizeTools(m);
			break;
		}

		case 'confirmation': {
			const prompt = String(payload.prompt ?? '');
			setTrailingText(m, prompt);
			m.text = prompt;
			m.confirmation = true;
			m.status = 'pending_confirmation';
			m.progress = undefined;
			m.streaming = false;
			finalizeTools(m);
			break;
		}

		case 'error': {
			const msg = String(payload.message ?? 'Something went wrong.');
			setTrailingText(m, msg);
			m.text = msg;
			m.error = true;
			m.progress = undefined;
			m.streaming = false;
			finalizeTools(m);
			break;
		}

		case 'cancelled':
			// No canonical result to reconcile. Mark a cancellation only when no
			// answer streamed in; otherwise keep the partial answer as-is.
			if (answerText(m).trim() === '') {
				m.segments.push({ kind: 'text', id: nextTextId(), text: '_(cancelled)_', settled: false });
			}
			m.text = answerText(m);
			m.progress = undefined;
			m.streaming = false;
			finalizeTools(m);
			break;

		case 'done':
			// Terminal safety net: if no result/error/cancelled arrived, still
			// stop streaming and freeze running tools.
			m.streaming = false;
			finalizeTools(m);
			if (typeof payload.duration_seconds === 'number') {
				m.durationSeconds = payload.duration_seconds;
			}
			if (typeof payload.model === 'string' && payload.model) {
				m.model = payload.model;
			}
			if (!m.text) m.text = answerText(m);
			break;
	}
}
