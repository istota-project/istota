/**
 * Messages typed into a busy room and waiting to be sent, kept per room in the
 * browser (ISSUE-238).
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
 * Nothing here decides whether an entry may go out. A restored entry is always
 * re-held by the caller — the turn it was written against is over and
 * unobserved, and a page load must never send anything by surprise.
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
 * A cap the UI can honestly report: past it the composer refuses with a
 * visible reason rather than accepting the text and silently dropping it.
 */
export const MAX_QUEUED_PER_ROOM = 10;

/** Second bound, on rooms rather than entries, evicted oldest-first. */
export const MAX_QUEUE_ROOMS = 20;

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
 * browser's quota, which is shared with everything else on the origin. Counted
 * in characters of message text rather than in the code units the serialized
 * payload costs; JSON escaping inflates that by a small constant factor and
 * the headroom below ~5 MB absorbs it.
 *
 * Must stay at or above `MAX_QUEUE_CHARS`, or the head of the room being
 * written could not fit and the newest-always-kept guarantee would not hold.
 * Note that one *room* at its own cap can exceed this, which is why the whole
 * map bound is applied entry by entry rather than room by room.
 */
export const MAX_QUEUE_TOTAL_CHARS = 256 * 1024;

/**
 * One queued message as it is stored.
 *
 * The same fields the in-memory entry carries. `held` and `queuedAt` are what
 * the restore reads: the first is forced true on the way back in by the chat
 * store, the second is what the TTL measures and what the restored row's
 * timestamp is rebuilt from.
 */
export interface StoredQueuedSend {
  cid: number;
  text: string;
  attachments: ChatAttachment[];
  replyTo?: MessageReply;
  replyToMsgId?: number;
  idempotencyKey?: string;
  held: boolean;
  queuedAt: number;
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
 * be sent as a file reference the server cannot resolve.
 */
function readAttachment(value: unknown): ChatAttachment | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const a = value as Partial<ChatAttachment>;
  if (typeof a.path !== 'string' || typeof a.name !== 'string') return null;
  return {
    path: a.path,
    name: a.name,
    size: Number.isFinite(a.size) ? (a.size as number) : 0,
    ...(typeof a.workspace_path === 'string' ? { workspace_path: a.workspace_path } : {}),
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
function readEntry(value: unknown): StoredQueuedSend | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const e = value as Partial<StoredQueuedSend>;
  // `Number.isFinite` rather than `typeof === 'number'`, as in `drafts.ts`:
  // every comparison against NaN is false, so such an entry would read back
  // yet never expire, and an infinite `queuedAt` would sort to the front of
  // the eviction order forever.
  if (!Number.isFinite(e.cid) || typeof e.text !== 'string') return null;
  if (!Number.isFinite(e.queuedAt) || !Array.isArray(e.attachments)) return null;
  const attachments: ChatAttachment[] = [];
  for (const raw of e.attachments) {
    const a = readAttachment(raw);
    if (!a) return null;
    attachments.push(a);
  }
  const replyTo = readReplyTo(e.replyTo);
  return {
    cid: e.cid as number,
    // Clamped on the way in as well as on the way out, so an entry written
    // before the cap existed cannot be read back at full size and re-stored.
    text: clampText(e.text),
    attachments,
    ...(replyTo ? { replyTo } : {}),
    ...(Number.isFinite(e.replyToMsgId) && (e.replyToMsgId as number) > 0
      ? { replyToMsgId: e.replyToMsgId as number }
      : {}),
    ...(typeof e.idempotencyKey === 'string' ? { idempotencyKey: e.idempotencyKey } : {}),
    held: e.held === true,
    queuedAt: e.queuedAt as number,
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
      const entry = readEntry(item);
      if (!entry || now - entry.queuedAt >= QUEUE_TTL_MS) continue;
      entries.push(entry);
    }
    if (entries.length) out[key] = entries;
  }
  return out;
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
      if (total + entry.text.length > MAX_QUEUE_TOTAL_CHARS) {
        full = true;
        break;
      }
      total += entry.text.length;
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
