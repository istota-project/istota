/**
 * Messages that cannot be sent yet and are waiting to be, kept per room in the
 * browser: typed into a busy room (ISSUE-238), or written with no connection
 * (ISSUE-202).
 *
 * Deliberately shaped like `drafts.ts`, and for the same reasons: one
 * `localStorage` map under a single key, so pruning is a local decision rather
 * than a scan of the whole origin's storage; keys are opaque strings the chat
 * store builds as `<user>:room:<token>`, by **token** because
 * `web_chat_rooms.id` is an `INTEGER PRIMARY KEY` without `AUTOINCREMENT` and
 * SQLite hands a freed rowid straight back out, and with the **user** in front
 * because a shared Talk room has one token across every member of a browser
 * profile.
 *
 * What differs from a draft is what the text means. A draft is half a thought;
 * a queued message is one the user has committed to sending, so losing it to a
 * reload is worse. It persists at enqueue rather than on unload, so nothing
 * depends on catching a departure event.
 *
 * An entry's *bytes* are the one thing kept elsewhere: a voice note recorded
 * with no connection lives in the `blobs` object store in IndexedDB and the
 * entry carries a `PendingAttachment` naming it. See that type for why the
 * split is where it is.
 *
 * What this module decides about sending is one thing and one only: how old an
 * entry queued with no connection may be and still go out by itself on a page
 * load (`OFFLINE_AUTO_SEND_MAX_AGE_MS`). Everything else — whether the room is
 * ready, whether the head is held, when a drain runs — is the chat store's,
 * and the rule it applies is that a restored entry sends itself only when it
 * was written against a *connection* rather than against a turn, and only
 * while it is young enough to still be what the user meant.
 *
 * **Two tabs are last-writer-wins, and that is settled rather than overlooked.**
 * Each tab holds its own in-memory queue and writes a room's whole list here,
 * so a write from one replaces what the other stored for that room; nothing
 * listens for the `storage` event and the map carries no version. The failure
 * that buys is a lost or duplicated *restore* across a reload, not a duplicated
 * send — `idempotencyKey` is minted at enqueue and rides the round trip, so the
 * server answers a second POST of the same entry with the first task. Multi-tab
 * chat is not synchronized anywhere else either, and `drafts.ts` has the same
 * shape. Note the distinction from the user in the key above: that separates
 * two *people* sharing a profile, which is a correctness boundary; this is one
 * person with two tabs, which is not.
 */
import { loadSetting, saveSetting } from './persisted';
import { MAX_DRAFT_CHARS } from './drafts';
import type { ChatAttachment } from '$lib/api';
import type { MessageReply } from './segments';

export const SEND_QUEUE_STORAGE_KEY = 'chat.sendQueue';

/**
 * How long a queued message survives unsent.
 *
 * Shorter than the draft TTL (a month) on purpose: a queued message is a
 * pending *action*, and one that has been pending since last season should not
 * still be waiting to fire the moment somebody taps Send on it.
 */
export const QUEUE_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/**
 * How many messages one room may have waiting.
 *
 * A cap the UI can honestly report: past it the send is refused with a visible
 * reason rather than accepted and silently dropped. `chat.ts` enforces it on
 * both ways into the queue — `enqueueSend` and `parkSend` — so the in-memory
 * queue can never outgrow what is stored; the trim in `prune` below keeps the
 * FIFO *head*, which would otherwise discard the message the user typed most
 * recently while leaving it on screen. The composer gains its own refusal on
 * top, which keeps the text in the field rather than in a notice.
 */
export const MAX_QUEUED_PER_ROOM = 10;

/** Second bound, on rooms rather than entries, evicted oldest-first. */
export const MAX_QUEUE_ROOMS = 20;

/**
 * How old an entry queued *offline* may be and still send itself on a restore.
 *
 * A restored entry is normally held: the turn it was written against is over
 * and unobserved, and a page load must never send by surprise. An entry queued
 * because there was no connection is the opposite case — going out on its own
 * is the whole of what it is for — so the hold would be the surprise instead.
 *
 * The bound splits two hazards rather than picking one. A message written five
 * minutes ago in a lift is one the user still means. A message written four
 * days ago, firing into a conversation that has moved on while the user is
 * looking at something else, is exactly what the hold exists to prevent. Well
 * inside `QUEUE_TTL_MS`, which is when the text stops being kept at all.
 */
export const OFFLINE_AUTO_SEND_MAX_AGE_MS = 24 * 60 * 60 * 1000;

/**
 * How much of one queued message is kept.
 *
 * The same ceiling a draft gets, and imported rather than restated so the two
 * cannot drift. The relationship that matters is with the server's own limit
 * on a send (`[web.chat] max_prompt_chars`, 32,000 by default): as long as the
 * cap sits above what the server would accept, truncation can only ever reach
 * text that could not have been sent anyway.
 */
export const MAX_QUEUE_CHARS = MAX_DRAFT_CHARS;

/**
 * How much the whole map holds, across every room.
 *
 * The per-entry cap does not bound the map on its own — `MAX_QUEUE_ROOMS`
 * rooms of `MAX_QUEUED_PER_ROOM` full-size entries would add up far past a
 * browser's quota, which is shared with everything else on the origin.
 *
 * Counted over the **serialized entry**, not over its text. `drafts.ts` counts
 * text because a draft is text; an entry here also carries attachment records,
 * a citation excerpt and an idempotency key, and the thing the quota actually
 * charges for is the serialized map. Counting the text alone would leave every
 * one of those unbounded against a budget written to bound them.
 *
 * A held attachment costs its `PendingAttachment` record and nothing more: the
 * bytes are in IndexedDB, under bounds of their own, and putting them here
 * would put a voice note through `localStorage` as base64.
 *
 * Must stay at or above `MAX_QUEUE_CHARS`, or the head of the room being
 * written could not fit and the newest-always-kept guarantee would not hold.
 * Note that one *room* at its own cap can exceed this, which is why the whole
 * map bound is applied entry by entry rather than room by room.
 */
export const MAX_QUEUE_TOTAL_CHARS = 256 * 1024;

/**
 * Why a message is in the queue.
 *
 * The two are told apart on the way back in and nowhere else: a `busy` entry
 * restores held, an `offline` one young enough and not already held restores
 * ready to go (see `OFFLINE_AUTO_SEND_MAX_AGE_MS`). Storing the reason rather than deriving it
 * is what makes that possible at all — a reload cannot tell whether the room
 * was busy or the network was gone when the text was written.
 */
export type QueueReason = 'busy' | 'offline';

/**
 * One attachment whose bytes are still in this browser.
 *
 * An attachment is a two-step send — the file is POSTed first and the message
 * references the host path that comes back — so with no connection there is no
 * path to reference. The bytes go into the `blobs` object store in IndexedDB
 * and this is what names them: the drain reads the blob, uploads it, replaces
 * the entry's pending chip with the real `ChatAttachment`, and deletes the
 * blob.
 *
 * Bytes are deliberately *not* here. This map is `localStorage` and is read
 * synchronously during `init()`; a voice note in it would blow the whole-map
 * bound on its own and would have to be base64 to fit at all. What the bound
 * below charges for is this record — four small fields — which is the right
 * accounting, because the bytes have bounds of their own where they live
 * (`MAX_PENDING_BLOB_BYTES`, `MAX_PENDING_BLOB_TOTAL`).
 */
export interface PendingAttachment {
  /** Key into the `blobs` object store. */
  blobId: string;
  name: string;
  mimeType: string;
  size: number;
}

/**
 * One queued message as it is stored.
 *
 * The same fields the in-memory entry carries, and three of them are what a
 * restore reads. `held` is a hold the last session applied and is never
 * cleared by the read. `reason` and `queuedAt` decide whether an entry that
 * carries no such hold may go out on its own.
 *
 * `attachments` and `pendingAttachments` are two views of one list and are
 * kept in step: every chip whose `path` is null carries a `pendingBlobId`, and
 * that id has exactly one record in `pendingAttachments`. The drain resolves
 * them one at a time — a chip gets its real path, its record leaves the list —
 * so a half-resolved entry is an ordinary state and `readEntry` below refuses
 * one where the two halves disagree.
 */
export interface StoredQueuedSend {
  cid: number;
  text: string;
  attachments: ChatAttachment[];
  /** The unresolved half of `attachments`, in the order it will be uploaded. */
  pendingAttachments?: PendingAttachment[];
  replyTo?: MessageReply;
  replyToMsgId?: number;
  idempotencyKey?: string;
  held: boolean;
  queuedAt: number;
  reason: QueueReason;
}

type QueueMap = Record<string, StoredQueuedSend[]>;

/**
 * `text` cut to `MAX_QUEUE_CHARS`, without splitting a surrogate pair.
 *
 * A pair split down the middle leaves a lone high surrogate, which survives
 * JSON intact and then renders as a replacement character — so a truncated
 * message would come back with a visible artifact on the end rather than
 * simply stopping. Backing off one unit costs the emoji and nothing else.
 */
function clampText(text: string): string {
  if (text.length <= MAX_QUEUE_CHARS) return text;
  const cut = text.charCodeAt(MAX_QUEUE_CHARS - 1);
  const end = cut >= 0xd800 && cut <= 0xdbff ? MAX_QUEUE_CHARS - 1 : MAX_QUEUE_CHARS;
  return text.slice(0, end);
}

/**
 * One stored attachment, or null when it is not attachment-shaped.
 *
 * `path` is the host path the POST carries, so an attachment missing it would
 * be sent as a file reference the server cannot resolve — unless it carries a
 * `pendingBlobId` instead, which says the bytes are still in this browser and
 * the path is what the drain is going to fetch. Those are the only two shapes;
 * anything else fails the entry.
 */
function readAttachment(value: unknown): ChatAttachment | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const a = value as Partial<ChatAttachment>;
  if (typeof a.name !== 'string') return null;
  const blobId = typeof a.pendingBlobId === 'string' && a.pendingBlobId ? a.pendingBlobId : null;
  if (typeof a.path !== 'string' && !blobId) return null;
  return {
    path: typeof a.path === 'string' ? a.path : null,
    name: a.name,
    size: Number.isFinite(a.size) ? (a.size as number) : 0,
    ...(blobId ? { pendingBlobId: blobId } : {}),
    ...(typeof a.mimeType === 'string' ? { mimeType: a.mimeType } : {}),
    ...(typeof a.workspace_path === 'string' ? { workspace_path: a.workspace_path } : {}),
  };
}

/** One stored blob reference, or null when it is not reference-shaped. */
function readPendingAttachment(value: unknown): PendingAttachment | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const p = value as Partial<PendingAttachment>;
  if (typeof p.blobId !== 'string' || !p.blobId) return null;
  if (typeof p.name !== 'string' || !p.name) return null;
  return {
    blobId: p.blobId,
    name: p.name,
    mimeType: typeof p.mimeType === 'string' ? p.mimeType : '',
    size: Number.isFinite(p.size) ? (p.size as number) : 0,
  };
}

/** The optimistic quote, or undefined when it is not citation-shaped. */
function readReplyTo(value: unknown): MessageReply | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const r = value as Partial<MessageReply>;
  if (!Number.isFinite(r.msgId) || (r.msgId as number) <= 0) return undefined;
  const role = r.role === 'user' || r.role === 'assistant' || r.role === 'system' ? r.role : null;
  return {
    msgId: r.msgId as number,
    ...(role ? { role } : {}),
    ...(typeof r.excerpt === 'string' ? { excerpt: r.excerpt } : {}),
    ...(r.deleted === true ? { deleted: true } : {}),
  };
}

/**
 * One stored entry, validated and clamped, or null to drop it.
 *
 * Per-entry validation rather than a blanket cast: this text is POSTed and its
 * attachments are handed to the server as paths, so a hand-edited or
 * half-written payload should cost one message rather than the room's whole
 * queue. A malformed attachment fails the entry rather than being dropped out
 * of it — a message that goes out without the file it was written about is a
 * worse outcome than one the user has to retype.
 */
function readEntry(value: unknown, now: number): StoredQueuedSend | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const e = value as Partial<StoredQueuedSend>;
  // `Number.isFinite` rather than `typeof === 'number'`, as in `drafts.ts`:
  // every comparison against NaN is false, so such an entry would read back
  // yet never expire, and an infinite `queuedAt` would sort to the front of
  // the eviction order forever.
  if (!Number.isFinite(e.cid) || typeof e.text !== 'string') return null;
  if (!Array.isArray(e.attachments)) return null;
  if (e.pendingAttachments !== undefined && !Array.isArray(e.pendingAttachments)) return null;
  const pendingAttachments: PendingAttachment[] = [];
  for (const raw of e.pendingAttachments ?? []) {
    const p = readPendingAttachment(raw);
    // Fails the entry rather than being dropped out of it, exactly as a
    // malformed attachment does: the message was written about the file, and
    // sending it without one is worse than making the user record it again.
    if (!p) return null;
    pendingAttachments.push(p);
  }
  // Blank text with nothing attached is not a message. `send()` trims and the
  // composer refuses an empty field, so this can only arrive hand-edited —
  // and restoring it would put a bubble on screen whose Send posts nothing,
  // the same call `writeDraft` makes about a whitespace-only draft. Text is
  // *not* required on its own: a send carrying only attachments is accepted
  // by the endpoint, which describes it in the prompt, so an attachment-only
  // entry is an ordinary queued message.
  if (e.text.trim() === '' && e.attachments.length === 0 && pendingAttachments.length === 0) {
    return null;
  }
  // Finite *and* inside the range `Date` can represent: `queuedRow` builds the
  // row's timestamp with `new Date(queuedAt).toISOString()`, which throws
  // RangeError past ±8.64e15 — and it throws inside the transcript rebuild, so
  // one hand-edited number would cost the whole room's history load rather
  // than its own entry.
  if (!Number.isFinite(e.queuedAt) || Math.abs(e.queuedAt as number) > 8.64e15) return null;
  const attachments: ChatAttachment[] = [];
  for (const raw of e.attachments) {
    const a = readAttachment(raw);
    if (!a) return null;
    attachments.push(a);
  }
  // The two halves have to name each other exactly. A chip with no path and no
  // record behind it would be POSTed as a null path the server cannot resolve;
  // a record with no chip would upload a file nothing references. Both are the
  // shape a half-written or hand-edited map has, and both cost the entry.
  const chipIds = attachments.map((a) => a.pendingBlobId).filter((id): id is string => !!id);
  const recordIds = pendingAttachments.map((p) => p.blobId);
  if (chipIds.length !== recordIds.length) return null;
  if (chipIds.some((id) => !recordIds.includes(id))) return null;
  const replyTo = readReplyTo(e.replyTo);
  // The two citation fields are one fact — `enqueueSend` writes
  // `replyToMsgId: replyTo?.msgId` — so they are recovered as a pair rather
  // than independently. Validated apart, a half-surviving pair either sends a
  // reply stripped of the parent it was written against (the failure
  // `editQueued` exists to prevent) or renders a quote the POST does not
  // carry. Where only one survives, the other is derived from it, so the
  // citation is kept in both directions.
  const replyToMsgId =
    Number.isFinite(e.replyToMsgId) && (e.replyToMsgId as number) > 0
      ? (e.replyToMsgId as number)
      : replyTo?.msgId;
  return {
    cid: e.cid as number,
    // Clamped on the way in as well as on the way out, so an entry written
    // before the cap existed cannot be read back at full size and re-stored.
    text: clampText(e.text),
    attachments,
    ...(pendingAttachments.length ? { pendingAttachments } : {}),
    ...(replyTo ? { replyTo } : {}),
    ...(replyToMsgId ? { replyToMsgId } : {}),
    ...(typeof e.idempotencyKey === 'string' ? { idempotencyKey: e.idempotencyKey } : {}),
    held: e.held === true,
    // Anything that is not the word `offline` is a busy entry, which is what
    // an entry written before this field existed is: the queue only had the
    // one reason then. Defaulting the other way would take every stored entry
    // on an upgraded build and send it unasked on the next load.
    reason: e.reason === 'offline' ? 'offline' : 'busy',
    // Never in the future. A stamp ahead of the clock outlives the TTL by
    // construction (`now - queuedAt` stays negative) and sorts its room to the
    // front of the eviction order for as long as it is stored — immortal, and
    // ahead of every real message. A wrong clock at write time, later
    // corrected, reaches this without anyone hand-editing anything, so it is
    // clamped rather than refused: the entry ages from now instead of never.
    queuedAt: Math.min(e.queuedAt as number, now),
  };
}

/**
 * The stored map, with anything that is not queue-shaped dropped and anything
 * past the TTL expired.
 *
 * Expiry is applied here rather than only on write so every caller shares one
 * notion of what is stored: a stale entry is never restored, whether or not
 * some other room has written since it aged out.
 */
function readAll(now = Date.now()): QueueMap {
  const raw = loadSetting<unknown>(SEND_QUEUE_STORAGE_KEY, null);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const out: QueueMap = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!Array.isArray(value)) continue;
    const entries: StoredQueuedSend[] = [];
    for (const item of value) {
      const entry = readEntry(item, now);
      if (!entry || now - entry.queuedAt >= QUEUE_TTL_MS) continue;
      entries.push(entry);
    }
    if (entries.length) out[key] = entries;
  }
  return out;
}

/**
 * What one entry costs the whole-map budget: its serialized length.
 *
 * The map is what the quota charges for, and an entry is more than its text —
 * attachment records, a citation excerpt, an idempotency key. Text-only
 * accounting would leave all of those outside a bound written to hold them.
 */
function entryCost(entry: StoredQueuedSend): number {
  return JSON.stringify(entry).length;
}

/** A room's age for eviction: when anything was last queued into it. */
function roomAt(entries: StoredQueuedSend[]): number {
  let newest = 0;
  for (const entry of entries) if (entry.queuedAt > newest) newest = entry.queuedAt;
  return newest;
}

/**
 * Expired entries out, then the newest rooms kept until a bound is reached.
 *
 * `keep` is the room the current write is about, and it goes to the front
 * whatever its age. Ordering by timestamp alone is not enough: two writes in
 * the same millisecond tie, a tie resolves to insertion order, and the room a
 * caller just wrote could then be the one evicted — the single outcome no
 * caller can work around.
 *
 * Within a room the order is FIFO and the *head* is what survives the per-room
 * cap: the head is what drains next, so trimming from the front would reorder
 * what the user wrote and send the wrong message first.
 */
function prune(map: QueueMap, now: number, keep?: string): QueueMap {
  const live: [string, StoredQueuedSend[]][] = [];
  for (const [key, entries] of Object.entries(map)) {
    const kept = entries
      .filter((e) => now - e.queuedAt < QUEUE_TTL_MS)
      .slice(0, MAX_QUEUED_PER_ROOM);
    if (kept.length) live.push([key, kept]);
  }
  live.sort((a, b) => roomAt(b[1]) - roomAt(a[1]));
  const first = keep === undefined ? -1 : live.findIndex(([key]) => key === keep);
  if (first > 0) live.unshift(...live.splice(first, 1));

  const out: QueueMap = {};
  let total = 0;
  let full = false;
  for (const [key, entries] of live.slice(0, MAX_QUEUE_ROOMS)) {
    if (full) break;
    const kept: StoredQueuedSend[] = [];
    for (const entry of entries) {
      // Stop rather than skip, as in `drafts.ts`: rooms are already in
      // eviction order, so passing over one that does not fit to reach a
      // smaller, older one would keep the wrong messages.
      if (total + entryCost(entry) > MAX_QUEUE_TOTAL_CHARS) {
        full = true;
        break;
      }
      total += entryCost(entry);
      kept.push(entry);
    }
    if (kept.length) out[key] = kept;
  }
  return out;
}

/** The queue held under `key`, in the order it will drain. */
export function readQueue(key: string): StoredQueuedSend[] {
  return readAll()[key] ?? [];
}

/** Every stored queue, for the chat store's restore at `init()`. */
export function readAllQueues(): QueueMap {
  return readAll();
}

/**
 * Remove one room's queue outright.
 *
 * Same effect as writing an empty list, and named separately because the
 * intent differs: this is the room going away, not the last entry draining.
 */
export function dropQueue(key: string): void {
  const map = readAll();
  if (!(key in map)) return;
  delete map[key];
  saveSetting(SEND_QUEUE_STORAGE_KEY, prune(map, Date.now()));
}

/**
 * Hold `entries` under `key`, or drop the room's entry when there are none.
 *
 * Returns what is actually stored under `key`, which is `entries` with each
 * text clamped and the bounds above applied — for the reason `writeDraft`
 * returns its clamped text: a caller keeping its own copy and comparing
 * against the stored one would otherwise miss on every later comparison.
 * The chat store deliberately does not adopt it, because the live queue is the
 * in-memory one and a storage bound must not delete a message the user can see
 * on screen; see `persistRoomQueue` there.
 */
export function writeQueue(key: string, entries: StoredQueuedSend[]): StoredQueuedSend[] {
  const now = Date.now();
  const map = readAll(now);
  if (entries.length === 0) {
    if (key in map) {
      delete map[key];
      saveSetting(SEND_QUEUE_STORAGE_KEY, prune(map, now));
    }
    return [];
  }
  map[key] = entries.map((entry) => ({ ...entry, text: clampText(entry.text) }));
  const pruned = prune(map, now, key);
  saveSetting(SEND_QUEUE_STORAGE_KEY, pruned);
  return pruned[key] ?? [];
}
