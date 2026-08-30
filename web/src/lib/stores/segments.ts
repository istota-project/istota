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
  | { kind: 'tool'; id: string; tool: ToolEntry }
  // An out-of-band notice about the run itself rather than about its content —
  // today only a brain fallback (ISSUE-278). It sits in the segment list so it
  // renders at the point in the turn where it happened, and it is never the
  // answer: `answerText` and `setTrailingText` both look for `text` segments
  // only, so a notice can neither become the answer nor be overwritten by it.
  //
  // LIVE-ONLY, deliberately. A finished turn is rebuilt from the brain's
  // `execution_trace` (`web_app._trace_segments` → `historySegments`), which
  // has no notice in it, so a reload shows the answer without the notice that
  // preceded it. Persisting one would mean writing into the trace contract that
  // `_compose_full_result` also reads, which is a bigger change than the
  // reported failure needs: the durable record of a model substitution is the
  // italic note `executor._append_model_note` appends to the reply on a dropped
  // pin, plus the model in the turn's meta from the `done` event.
  | { kind: 'notice'; id: string; text: string };

// One structured !search result. Mirrors the backend `_build_search_data`
// per-result shape; a conversation card carries the `task_id` the client jumps
// to, a memory card carries none.
export interface SearchResultItem {
  source_type: string;
  summary: string;
  date: string;
  room_token: string | null;
  room_name: string | null;
  task_id: number | null;
  talk_message_id: number | null;
  talk_link: string | null;
}

// The `command_data` payload a !search inline command produces (kind ===
// 'search_results'). Rendered as result cards instead of the markdown fallback.
export interface SearchResultsData {
  kind: 'search_results';
  query: string;
  results: SearchResultItem[];
  // Plain-text fallback (the transcript's durable record).
  text: string;
}

// The `command_data` payload a bare "yes"/"no" answering a parked confirmation
// produces (ISSUE-243). Unlike every other inline result this exchange is
// durable — the server wrote both halves into `messages` — so it carries their
// canonical ids for the client to stamp onto the rows already on screen. Without
// that the room stream's own echo of them appends a second copy of each.
export interface ConfirmationAnsweredData {
  kind: 'confirmation_answered';
  user_msg_id: number | null;
  system_msg_id: number | null;
}

// `!steer`'s payload (ISSUE-300), durable for the same reason and needing the
// same stamp: `cmd_steer` records the note as a `task_id IS NULL` user row, so
// the room stream echoes it back with `msg_id` as the only available dedup key.
// `body` is the note as stored — the client drew the whole `!steer <note>` line
// it was given, and adopting the stored body is what keeps the live transcript
// and a reloaded one showing the same bubble.
export interface SteerRecordedData {
  kind: 'steer_recorded';
  user_msg_id: number | null;
  body: string;
}

/**
 * The POST body a Retry replays.
 *
 * Declared structurally rather than importing `ChatAttachment` from `$lib/api`:
 * this module is deliberately free of SvelteKit imports so the reducer
 * unit-tests without a DOM.
 */
/**
 * An uploaded attachment, exactly as `api.ts`'s `ChatAttachment`.
 *
 * Restated structurally rather than imported: this module is deliberately free
 * of SvelteKit-dependent imports (see the header), and `api.ts` reaches for
 * `$app/paths`.
 */
export interface SendAttachment {
  /**
   * Null while the bytes are still in this browser, waiting for a connection
   * (ISSUE-202). A payload can hold one because a queued message's payload is
   * built before its files exist server-side; `beginSend` resolves every one of
   * them to a real path before the POST, and `sendTurn` drops any that is
   * somehow still null rather than asking the server to read a missing file.
   */
  path: string | null;
  name: string;
  size: number;
  workspace_path?: string | null;
  /** Key into the offline `blobs` store, on a pending chip and nowhere else. */
  pendingBlobId?: string;
  /** The picked file's type, carried only while the file is still held here. */
  mimeType?: string;
}

export interface SendPayload {
  text: string;
  /**
   * The whole attachment, not the `{path, name}` projection the POST needs.
   * A retry re-enters the same code path a first send takes, so it has to hand
   * over the same shape — fabricating the missing fields (`size: 0`) put a
   * value through the composer's size check that was not the file's.
   */
  attachments: SendAttachment[];
  /**
   * Client-minted identity for this message, carried by every attempt at it.
   *
   * A retry of a send the server had in fact accepted (a client-side timeout,
   * a socket dropped after the request was processed) would otherwise create a
   * second task and a second bubble. The server recognises the key and hands
   * back the first task instead, so the duplicate never exists — which is the
   * thing the body-match adoption in `appendStreamedRow` can only detect, and
   * only for a body the server did not rewrite.
   */
  idempotencyKey?: string;
  /**
   * Canonical id of the cited parent, so a retry re-POSTs the same citation.
   * `retrySend` re-POSTs from `sendPayload` alone, so leaving it out here is
   * how a retry silently turns a reply into an ordinary message.
   */
  replyToMsgId?: number;
}

/**
 * Display cap on a citation's excerpt, matching the server's own.
 *
 * The staged chip is built client-side from the transcript while the rendered
 * quote comes back from the server, so the two have to agree or a reply would
 * show one excerpt while composing and a different one after a reload.
 */
export const REPLY_EXCERPT_CHARS = 200;

/**
 * The parent a turn replies to, as the transcript renders it.
 *
 * The client-side spelling of the server's `reply_to`. A `deleted` parent
 * carries only its id and renders muted and inert.
 */
export interface MessageReply {
  msgId: number;
  role?: 'user' | 'assistant' | 'system';
  excerpt?: string;
  /**
   * Absent counts as live, so the composer can stage a citation without
   * asserting anything about a state only the server knows. Reply is only ever
   * offered on a durable row that is on screen, so there is no staged-citation
   * case where this would be true.
   */
  deleted?: boolean;
}

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
  // Positional against `attachments`: the workspace path each chip opens, or
  // null for one the file endpoint can't serve (another member's upload, a
  // deployment with no local workspace) — that chip stays inert.
  attachmentPaths?: (string | null)[];
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
  // System rows only: when a !search command produced structured results, the
  // row renders result cards from this instead of its markdown `text`.
  searchResults?: SearchResultsData;
  // The message this turn replies to, rendered as a quote above the body.
  replyTo?: MessageReply;
  // User rows only: who wrote it, when that is not the viewer. A room is
  // shared and an email turn is mirrored into the room it continues, so a user
  // bubble is not necessarily the reader's own words. Absent → label with the
  // viewer's display name, the long-standing behaviour.
  author?: string;
  // User rows only: the surface the turn entered from, when it is not one the
  // room itself lives on — today `'email'` alone. Absent means it came from
  // inside the conversation, which is every web and Talk turn, so the external
  // treatment keys on presence rather than on a comparison.
  origin?: string;
  // The subject line of an external turn's mail. What a collapsed turn shows in
  // place of the body, since the body itself is what is being withheld.
  subject?: string;

  // ---- Send lifecycle (ISSUE-200) -------------------------------------------
  // User rows this client originated, only. Absent means settled — which every
  // row rebuilt from history is, so history construction needs no new field.
  //
  // There is deliberately no 'sent' member. The visible confirmation of the
  // backend's ack is the pending mark *clearing*; a permanent receipt on every
  // outbound row would be clutter saying something we don't actually know (we
  // have no read receipts to be consistent with).
  //
  // 'queued' is the send queue's own state (ISSUE-238): the message has been
  // written and committed to, and has not been POSTed at all. It is a
  // different thing from the assistant placeholder's `Queued…` progress line,
  // which means the opposite — the turn is on the server and is waiting for
  // its stream.
  sendState?: 'sending' | 'failed' | 'queued';
  // Only meaningful on a 'queued' row: the entry will not drain on its own.
  // Set when the turn the message was written against ended abnormally
  // (Stop, an error, a parked confirmation, a failed send), and on every entry
  // restored from storage past its auto-send age. Cleared by releasing the
  // entry.
  queueHeld?: boolean;
  // Why the row is queued, mirrored off its queue entry (ISSUE-202). The row
  // says which of the two waits it is in — for a running turn, or for a
  // connection — and that is the whole of what it is read for here; the
  // decision it drives (whether a restored entry may send itself) is made
  // against the stored entry rather than against this.
  queueReason?: 'busy' | 'offline';
  // Render gate for the pending mark, opened by a grace timer rather than by
  // `sendState` itself. The state is true from the moment the row exists; the
  // mark only earns the screen once the send is slow enough to be worth
  // reporting. See SEND_PENDING_GRACE_MS.
  showSending?: boolean;
  // Why it failed, rendered beside the marker. A sentence rather than a code
  // because the rate-limit case has to carry its own number.
  sendError?: string;
  // Whether Retry can succeed. False for an expired session, where the
  // affordance would lie.
  retryable?: boolean;
  // What a Retry re-POSTs. Kept because the rendered row holds display names
  // and workspace paths, not the host paths the POST body takes — deriving it
  // back from the row would silently drop attachments.
  sendPayload?: SendPayload;
}

// ---- Client-only rows -------------------------------------------------------
//
// The two rows the server has no copy of. Here rather than inside the chat
// session because the transcript renders them too (ISSUE-351): the store keeps
// them at the tail, and the page has to know not to date them.

/** Client-only without ambiguity: a server row always carries a `msgId`. */
export const isStranded = (m: ChatMessage) => m.sendState === 'failed' && m.msgId === undefined;

/** A message typed into a busy room: written, committed to, never POSTed. */
export const isQueued = (m: ChatMessage) => m.sendState === 'queued';

/**
 * A row the server has no copy of, and so one a rebuild has to carry rather
 * than drop.
 *
 * These are pending actions rather than events in the history — they carry
 * Send / Edit / Remove or a Retry, and they belong against the composer. The
 * store keeps them at the tail of the transcript for that reason, which is
 * also why they are not part of its chronology: a queued row is stamped when
 * it was typed and a restored one keeps that stamp for up to a week, so
 * anything reading `createdAt` off consecutive rows has to skip them or it
 * reports a day boundary at the bottom of a transcript that is otherwise
 * today's.
 */
export const isClientOnly = (m: ChatMessage) => isStranded(m) || isQueued(m);

// ---- Helpers ----------------------------------------------------------------

let _textSegSeq = 0;
function nextTextId(): string {
  return `s${++_textSegSeq}`;
}

let _thinkSegSeq = 0;
function nextThinkId(): string {
  return `k${++_thinkSegSeq}`;
}

let _noticeSegSeq = 0;
function nextNoticeId(): string {
  return `n${++_noticeSegSeq}`;
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

export function findTool(
  m: ChatMessage,
  id: string,
): Extract<Segment, { kind: 'tool' }> | undefined {
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
  // A notice has no settled/unsettled life — it is emitted complete and stays.
  if (seg.kind === 'notice') return seg.text.trim() !== '';
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
  | { kind: 'activity'; id: string; steps: Segment[] }
  | { kind: 'notice'; id: string; text: string };

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
    if (s.kind === 'notice') {
      // Always rendered, however short, and never coalesced into a chip: it is
      // the one thing in the turn the reader has to see without expanding
      // anything. Flushing first keeps it in true segment order.
      if (s.text.trim()) {
        flushTools();
        groups.push({ kind: 'notice', id: s.id, text: s.text });
      }
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

/** The whole of a message as markdown source, for a turn-level copy.
 *
 * Prose only: the activity chips are dropped, because a tool trace is never
 * something anyone wants on the clipboard, and the groups are exactly the
 * blocks the transcript rendered — so what lands on the clipboard is what the
 * reader saw, in the order they saw it. Blocks are joined by a blank line so
 * the result is still valid markdown rather than two paragraphs run together.
 *
 * A user or system row has no segments; its `text` is the whole message.
 *
 * Source, not rendered html: pasting elsewhere should give back the markdown
 * the message was written in. That is also why fenced code needs no copy
 * button of its own — the source carries its fences. */
export function messageCopyText(m: ChatMessage): string {
  if (m.role !== 'assistant') return m.text;
  return renderGroups(m)
    .filter((g): g is Extract<RenderGroup, { kind: 'prose' }> => g.kind === 'prose')
    .map((g) => g.text.trim())
    .filter(Boolean)
    .join('\n\n');
}

// ---- Reducer ----------------------------------------------------------------

/** Apply one `task_event` to an assistant message, mutating it in place.
 *
 * `task_started`'s ack-verb seeding is NOT handled here — it lives in chat.ts
 * (it's message state, not a segment, and competes with the client-side seed).
 * Its segment half is below. Unknown kinds are ignored. Missing payload fields
 * coerce to defaults. */
export function applyEvent(m: ChatMessage, kind: string, payload: Record<string, unknown>): void {
  switch (kind) {
    case 'task_started':
      // ISSUE-361: a retry runs under the same message. A retry-eligible
      // failure emits no terminal frame — only a `progress_text`, which
      // settles nothing — so without this the next attempt's text continues
      // the block the failed one left open, and two attempts read as one
      // paragraph. The `brain_fallback` notice used to settle it as a side
      // effect, which stopped once that notice became once-per-turn; a retry
      // is the boundary either way, whether or not it also failed over.
      // A no-op on the first attempt, where nothing is open.
      settleOpenBlock(m);
      break;

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

    case 'brain_fallback': {
      // ISSUE-278: the primary brain failed (or was already cooling down) and
      // the run continues elsewhere, possibly on a different model. Rendered
      // where it happened rather than folded into the answer, so the reader
      // learns it during the wait it explains and not after.
      const text = String(payload.text ?? '');
      // Trimmed, not truthy: a whitespace-only text renders nowhere (see
      // `renderGroups`) but would still settle the open block and so cost the
      // turn a paragraph break with nothing to show for it.
      if (!text.trim()) break;
      // Settle whatever was streaming: the notice interrupts the turn, and an
      // open text block that resumed after it would read as one paragraph.
      settleOpenBlock(m);
      m.segments.push({ kind: 'notice', id: nextNoticeId(), text });
      // `m.progress` is deliberately LEFT ALONE. The fallback run is the long
      // part of the wait, and the notice is a static sentence — clearing the
      // ack verb here would take away the turn's only live cue at exactly the
      // moment the reader needs it, which is the complaint ISSUE-278 opens
      // with. Message.svelte keeps the pulsing dot alive under a turn whose
      // only groups are notices.
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
      // Durable-store id for the just-settled turn (ISSUE-172): light up the
      // star affordance without waiting for a history refetch. A turn only
      // becomes starrable now that it is committed — so seed `starred` false
      // when it's the first time we learn the id.
      if (typeof payload.msg_id === 'number') {
        m.msgId = payload.msg_id;
        if (m.starred === undefined) m.starred = false;
      }
      if (!m.text) m.text = answerText(m);
      break;
  }
}
