/**
 * The offline read cache: one IndexedDB database, wrapped so that nothing it
 * does can break a render (ISSUE-202).
 *
 * The app is a static SPA whose every byte of data comes over the wire, so
 * with no connection there is nothing to read. This is where the last thing
 * the server said is kept, per room, so the transcript paints before — and
 * without — a fetch.
 *
 * **What is stored is the wire row, not the derived one.** `ChatHistoryMessage`
 * is exactly what `getRoomMessages` returns *and* exactly what a room-stream
 * `message` frame carries (`ChatRoomEvent = ChatHistoryMessage`), so both write
 * paths produce one shape, and it is read back through the one existing builder
 * in `chat.ts`. A cached row therefore cannot render differently from a fetched
 * one, and a change to how a turn renders is not a storage migration.
 *
 * **Every failure is swallowed and reported as an empty cache**, exactly as
 * `persisted.ts` swallows a `localStorage` refusal. Private mode, a quota
 * refusal, a corrupt database, a browser with no `indexedDB` at all: each read
 * resolves empty and each write resolves having done nothing. A broken cache
 * degrades to the behaviour the app had before this file existed; it never
 * breaks a send or a render, and no caller has an error path to write.
 *
 * **Keyed by room token, not room id, and namespaced by user.** The same two
 * reasons `drafts.ts` and `sendQueue.ts` give: `web_chat_rooms.id` is an
 * `INTEGER PRIMARY KEY` without `AUTOINCREMENT`, so SQLite hands a freed rowid
 * straight back out, and a shared Talk room has one token across every member
 * of a browser profile. The key carries the same `:room:` infix those two use,
 * and for the reason they have it: `<user>:<token>` lets one pair spell
 * another's key — user `a` in room `b:tok` and user `a:b` in room `tok` are
 * one entry — which is a cross-user read on a profile two people share. The
 * infix narrows that to a user id containing `:room:`, which a username cannot;
 * both sibling stores rest on the same assumption.
 *
 * The database is created at version 1 with all four stores. The `blobs` store
 * holds the bytes of an attachment written with no connection — a voice note,
 * a photo — until the outbox can upload it; the send queue in `localStorage`
 * holds the reference. Bytes here, references there, for the reason the two
 * stores are split at all: the queue must be readable synchronously during
 * `init()`, and a Blob must not be.
 */
import type { ChatConfig, ChatHistoryMessage, ChatRoom, User } from '$lib/api';

export const DB_NAME = 'istota-offline';
export const DB_VERSION = 1;

export const STORE_TRANSCRIPTS = 'transcripts';
export const STORE_ROOMS = 'rooms';
export const STORE_CONFIG = 'config';
export const STORE_BLOBS = 'blobs';

/**
 * How much of one room is kept.
 *
 * One page, which is `getRoomMessages`'s own default — so a cached room paints
 * exactly what opening it online would have painted, and there is no second
 * notion of "a transcript" for the offline case to get wrong. The cache is a
 * tail sized to pick up the thread, not offline history: paging older is a
 * fetch, and offline it is refused rather than half-served.
 */
export const CACHE_MESSAGES_PER_ROOM = 50;

/** How many rooms are kept at once, evicted least-recently-saved first. */
export const MAX_CACHED_ROOMS = 20;

/**
 * How much one room may cost, measured over its serialized rows.
 *
 * Tool segments on a long agent turn are not small, and one pathological room
 * must not eat a budget the whole origin shares — `localStorage`, IndexedDB and
 * the Cache API are evicted together.
 *
 * Measured in UTF-16 code units, as `sendQueue.ts` measures its own map, so the
 * number is approximate rather than a byte count: `JSON.stringify` leaves
 * non-ASCII unescaped, so a CJK or emoji-heavy transcript costs more bytes than
 * this counts and is let a little past the line. Approximate is enough for what
 * this is for — stopping one room spending the whole budget — and the exact
 * measure would mean encoding every row to count it, on every write.
 */
export const MAX_ROOM_CACHE_BYTES = 512 * 1024;

/**
 * How long anything cached stays worth painting.
 *
 * Long, because the cost of a stale paint is low — the fetch that follows it
 * replaces it wholesale, and offline the alternative is a blank room. It is a
 * floor under "this device has not been online in a month", where what is
 * stored has stopped being a useful picture of the conversation.
 */
export const CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000;

/**
 * The largest single file the outbox will hold offline.
 *
 * A voice note is seconds of Opus or AAC and is nowhere near this; a 4K video
 * is well past it. Refusing that one with a sentence beats filling the origin's
 * quota with it — eviction is whole-origin, so a video that fills the budget
 * takes the text queue and the transcript cache down with it.
 *
 * The server's own `max_attachment_mb` still applies and the smaller of the two
 * wins: this bound is about what is safe to *hold*, not about what the server
 * would take.
 */
export const MAX_PENDING_BLOB_BYTES = 10 * 1024 * 1024;

/** The same budget across every blob held at once. */
export const MAX_PENDING_BLOB_TOTAL = 50 * 1024 * 1024;

/**
 * How much of the origin's quota may be in use before a blob write is refused.
 *
 * WebKit's storage policy gives a non-browser app up to 15% of disk from iOS 17,
 * which is generous, but the number is not fixed and asking is one line. What
 * this buys is that the *last* write before the quota is reached is refused
 * with a sentence rather than throwing somewhere the user cannot see.
 */
export const STORAGE_HEADROOM_FRACTION = 0.8;

/**
 * How old an unreferenced blob has to be before it is collected.
 *
 * The floor is what keeps the collection from racing a compose: a file staged
 * in the composer and not yet sent is referenced by nothing at all, because
 * the reference is written when the message is queued. An hour is far longer
 * than any compose and far shorter than the queue's own TTL.
 */
export const BLOB_GC_MIN_AGE_MS = 60 * 60 * 1000;

/** One room's cached tail. */
export interface CachedTranscript {
  roomId: number;
  roomToken: string;
  messages: ChatHistoryMessage[];
  oldestCursor: { ts: string; id: number } | null;
  savedAt: number;
}

/**
 * One attachment's bytes, waiting for a connection to carry them.
 *
 * Held as an `ArrayBuffer` rather than as the `Blob` the composer starts with.
 * WebKit would store a `Blob` as a reference to a file on disk, which is the
 * cheaper shape and the one to prefer if this were the only consideration — but
 * it is not testable: `fake-indexeddb`'s structured clone flattens a jsdom
 * `Blob` to an empty object, so a stored `Blob` cannot be read back in the
 * suite and every assertion about `getBlob` would be vacuous. A buffer clones
 * faithfully everywhere, and `MAX_PENDING_BLOB_BYTES` is what bounds the heap
 * cost of materializing one. The `File` is rebuilt at the point of upload.
 */
export interface StoredBlob {
  bytes: ArrayBuffer;
  name: string;
  mimeType: string;
  size: number;
  createdAt: number;
}

interface CachedRooms {
  rooms: ChatRoom[];
  savedAt: number;
}

interface CachedConfig {
  config: ChatConfig;
  savedAt: number;
}

interface CachedUser {
  user: User;
  savedAt: number;
}

const KEY_INFIX = ':room:';
const transcriptKey = (userId: string, roomToken: string) => `${userId}${KEY_INFIX}${roomToken}`;

/**
 * The `config` store's second key shape, for the signed-in user (ISSUE-354).
 *
 * A suffix rather than a store of its own: `DB_VERSION` is 1 and all four
 * stores were created at once precisely so no later stage needs a bump, which
 * is the one IndexedDB operation another open tab can block. `config` is a
 * key-value store and holds a second shape for nothing.
 *
 * Two key shapes in one store can be spelled by each other, and the guard is
 * the shape check rather than the key: a user literally named `alice:me` keys
 * its *config* where user `alice`'s cached identity goes. Neither reader can be
 * fooled by that — `readConfig` refuses a record with no `config` and `readUser`
 * one with no `user` — so a collision degrades to a null and can never produce
 * a cross-user identity read. What it does cost is that user's config cache,
 * silently overwritten. That is a narrower assumption than the transcript key
 * above, which needs a `:room:` inside the id rather than a `:me` at the end of
 * it, and it is the reason the shape checks are not optional here.
 */
const KEY_ME_SUFFIX = ':me';
const userKey = (userId: string) => `${userId}${KEY_ME_SUFFIX}`;

// ---- The database ---------------------------------------------------------

/**
 * How long any one storage operation may take before it counts as no cache.
 *
 * The one thing the swallow-everything discipline below does not cover on its
 * own: an IndexedDB request that fires no event at all. WebKit does that — a
 * page restored from the back/forward cache, a connection left behind by an
 * abnormal close, storage under pressure — and this ships inside a WKWebView,
 * which is the environment the whole feature is for. Without a bound, one such
 * request would hang `init()`'s awaited cache read and leave the chat page on
 * its loading state for the life of the session, which is a worse failure than
 * the one the cache exists to prevent.
 *
 * Three seconds because this is a local read racing a network request that has
 * already been issued: past that the cache has lost its only job, which is
 * being the thing that answers first.
 */
export const OFFLINE_DB_TIMEOUT_MS = 3000;

let dbPromise: Promise<IDBDatabase | null> | null = null;
// The connection the memo currently holds, so a lifecycle event fired by a
// *previous* connection cannot drop the live one's memo and leave two open.
let openDb: IDBDatabase | null = null;

/**
 * The open database, or null when this origin has no usable one.
 *
 * Memoized, because opening is the expensive half and every helper below wants
 * the same handle. The memo is dropped again when the connection goes away
 * under us — another tab upgrading the schema, or the browser closing the
 * connection to reclaim storage, which on iOS is a thing that happens rather
 * than a hypothetical. Without that, one eviction would leave every later call
 * holding a dead handle for the life of the page.
 *
 * Resolves rather than rejects, always. `null` is the whole error vocabulary
 * this module has, and every caller reads it as "no cache".
 */
export function openOfflineDb(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise;
  let opening: Promise<IDBDatabase | null>;
  opening = new Promise<IDBDatabase | null>((resolve) => {
    if (typeof indexedDB === 'undefined' || indexedDB === null) {
      resolve(null);
      return;
    }
    // One settle, whichever of the four paths gets there first, and a handle
    // arriving after we have given up is closed rather than leaked — an
    // abandoned connection would go on blocking the next version change.
    let settled = false;
    const finish = (db: IDBDatabase | null) => {
      if (settled) {
        db?.close();
        return;
      }
      settled = true;
      clearTimeout(timer);
      openDb = db;
      resolve(db);
    };
    const timer = setTimeout(() => {
      // Dropped from the memo as well as given up on, so a later call gets a
      // fresh attempt rather than a permanently dead cache.
      if (dbPromise === opening) dbPromise = null;
      finish(null);
    }, OFFLINE_DB_TIMEOUT_MS);
    let req: IDBOpenDBRequest;
    try {
      // Throws outright in a few real configurations — a Safari private window
      // and a WebView with storage disabled among them — rather than firing
      // `onerror`.
      req = indexedDB.open(DB_NAME, DB_VERSION);
    } catch {
      finish(null);
      return;
    }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_TRANSCRIPTS)) {
        const store = db.createObjectStore(STORE_TRANSCRIPTS);
        // The eviction order, so trimming to `MAX_CACHED_ROOMS` and expiring
        // past `CACHE_TTL_MS` both walk an index instead of reading every
        // room's rows to find out how old they are.
        store.createIndex('savedAt', 'savedAt');
      }
      if (!db.objectStoreNames.contains(STORE_ROOMS)) db.createObjectStore(STORE_ROOMS);
      if (!db.objectStoreNames.contains(STORE_CONFIG)) db.createObjectStore(STORE_CONFIG);
      if (!db.objectStoreNames.contains(STORE_BLOBS)) {
        // Out-of-line keys: the blob id is a uuid the outbox mints and holds in
        // its queue entry, so it is a name for the record rather than a field
        // of it. The age index is what a later garbage collection walks.
        const store = db.createObjectStore(STORE_BLOBS);
        store.createIndex('createdAt', 'createdAt');
      }
    };
    req.onsuccess = () => {
      const db = req.result;
      // Both guarded on this still being the connection the memo holds: a
      // handler belonging to a connection we have already replaced would
      // otherwise drop the live memo and leave two connections open.
      db.onversionchange = () => {
        db.close();
        if (openDb === db) {
          openDb = null;
          dbPromise = null;
        }
      };
      db.onclose = () => {
        if (openDb === db) {
          openDb = null;
          dbPromise = null;
        }
      };
      finish(db);
    };
    req.onerror = () => finish(null);
    // Another tab holding an older version open. Waiting could be forever, so
    // this session goes without a cache rather than hanging every read on it;
    // if the block clears later, `finish` closes the handle nobody is waiting
    // for any more.
    req.onblocked = () => finish(null);
  });
  dbPromise = opening;
  return opening;
}

/**
 * Run one transaction and resolve with whatever it produced, or `fallback`.
 *
 * `work` issues its requests and reports through `done`; the result is taken
 * from `oncomplete`, not from the last request's `onsuccess`, so a write has
 * actually landed by the time its caller continues and a read cannot report a
 * value the transaction then aborts.
 *
 * Requests are chained inside their own `onsuccess` handlers rather than
 * awaited between statements. Both work in a compliant implementation, but the
 * callback form cannot be broken by a caller's `await` slipping into the middle
 * of a read-modify-write and letting the transaction auto-commit underneath it.
 */
async function withTx<T>(
  stores: string[],
  mode: IDBTransactionMode,
  work: (tx: IDBTransaction, done: (value: T) => void) => void,
  fallback: T,
): Promise<T> {
  const db = await openOfflineDb();
  if (!db) return fallback;
  return new Promise<T>((resolve) => {
    let value = fallback;
    // Bounded for the same reason the open is: a transaction that never
    // completes and never errors would hang whoever awaited it, and nothing
    // above has an error path to take.
    let settled = false;
    const finish = (v: T) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(v);
    };
    const timer = setTimeout(() => finish(fallback), OFFLINE_DB_TIMEOUT_MS);
    let tx: IDBTransaction;
    try {
      tx = db.transaction(stores, mode);
    } catch {
      // A store missing (a database from a build that predates it) or the
      // connection already closing.
      finish(fallback);
      return;
    }
    tx.oncomplete = () => finish(value);
    tx.onerror = () => finish(fallback);
    tx.onabort = () => finish(fallback);
    try {
      work(tx, (v) => {
        value = v;
      });
    } catch {
      try {
        tx.abort();
      } catch {
        /* already gone */
      }
      finish(fallback);
    }
  });
}

// ---- Transcripts ----------------------------------------------------------

/**
 * One stored row, or null when it is not message-shaped.
 *
 * Lighter than `sendQueue.ts`'s per-entry validation, and deliberately so:
 * nothing here is POSTed. A cached row is only ever rendered, through the same
 * builder a fetched row goes through, so the fields that have to be right are
 * the ones that builder branches on. What this guards is a row written by a
 * build whose shape has since changed, not a hand edit — IndexedDB is not the
 * text file `localStorage` effectively is.
 */
function readRow(value: unknown): ChatHistoryMessage | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Partial<ChatHistoryMessage>;
  if (row.role !== 'user' && row.role !== 'assistant' && row.role !== 'system') return null;
  if (typeof row.text !== 'string' || typeof row.created_at !== 'string') return null;
  return row as ChatHistoryMessage;
}

/**
 * Whether two wire rows are the same turn.
 *
 * The stream re-sends a row as it progresses — a turn arrives running and
 * arrives again finished — so an append that only ever appended would store
 * the same turn several times over and spend the room's whole budget on one
 * exchange. Identity is the durable id where there is one, the task where
 * there is not (an aux turn), and the notification id for a bot-delivered row:
 * the same three keys the transcript's own dedup uses.
 */
function sameRow(a: ChatHistoryMessage, b: ChatHistoryMessage): boolean {
  if (typeof a.msg_id === 'number' && typeof b.msg_id === 'number') return a.msg_id === b.msg_id;
  if (typeof a.notif_id === 'number' && typeof b.notif_id === 'number') {
    return a.notif_id === b.notif_id;
  }
  if (typeof a.task_id === 'number' && typeof b.task_id === 'number') {
    return a.task_id === b.task_id && a.role === b.role;
  }
  return false;
}

/**
 * The tail of `rows` that fits both bounds, oldest dropped first.
 *
 * Order is the server's and is preserved: the newest is what the user opens the
 * room to see, so the count bound keeps the end of the array and the byte bound
 * eats into it from the front. A single row over the whole budget leaves
 * nothing, which is the honest answer — an empty cache is a state the reader
 * already handles, and half a turn is not. `writeTranscript` turns that into a
 * delete rather than an empty record.
 *
 * Measured once per row, accumulating from the newest backwards, rather than
 * re-serializing the whole array on each trim. The naive loop is quadratic in
 * rows that can each carry a full execution trace, and it runs on the main
 * thread inside an open transaction on every debounced flush of every room —
 * including rooms nobody is looking at.
 */
function boundRows(rows: ChatHistoryMessage[]): ChatHistoryMessage[] {
  const tail = rows.slice(-CACHE_MESSAGES_PER_ROOM);
  const kept: ChatHistoryMessage[] = [];
  let total = 0;
  for (let i = tail.length - 1; i >= 0; i--) {
    // Plus the separator the row costs inside the serialized array, so the
    // accumulated figure stays comparable with the whole-array measure the
    // bound is written against.
    const cost = JSON.stringify(tail[i]).length + 1;
    if (total + cost > MAX_ROOM_CACHE_BYTES) break;
    total += cost;
    kept.push(tail[i]);
  }
  return kept.reverse();
}

/** Drop the least recently saved transcripts until `MAX_CACHED_ROOMS` remain. */
function evictOldestTranscripts(tx: IDBTransaction): void {
  const index = tx.objectStore(STORE_TRANSCRIPTS).index('savedAt');
  // Counted on the index rather than the store, because the index is what the
  // cursor below can delete from: a record with no usable `savedAt` has no
  // index entry, so counting it would evict a *valid* transcript in its place.
  // Such a record is collected by `pruneOffline`, which walks the store itself.
  const counting = index.count();
  counting.onsuccess = () => {
    let over = counting.result - MAX_CACHED_ROOMS;
    if (over <= 0) return;
    const cursoring = index.openCursor();
    cursoring.onsuccess = () => {
      const cursor = cursoring.result;
      if (!cursor || over <= 0) return;
      cursor.delete();
      over -= 1;
      cursor.continue();
    };
  };
}

/**
 * A room's cached tail, or null when there is none worth painting.
 *
 * Expiry is applied here as well as in `pruneOffline`, so a reader never has to
 * know whether the prune has run yet: a month-old transcript is not painted
 * merely because the app has not been open long enough to collect it.
 */
export async function readTranscript(
  userId: string | null,
  roomToken: string | null,
  now = Date.now(),
): Promise<CachedTranscript | null> {
  if (!userId || !roomToken) return null;
  return withTx<CachedTranscript | null>(
    [STORE_TRANSCRIPTS],
    'readonly',
    (tx, done) => {
      const req = tx.objectStore(STORE_TRANSCRIPTS).get(transcriptKey(userId, roomToken));
      req.onsuccess = () => {
        const value = req.result as Partial<CachedTranscript> | undefined;
        if (!value || !Array.isArray(value.messages)) return;
        if (typeof value.savedAt !== 'number' || now - value.savedAt >= CACHE_TTL_MS) return;
        const messages: ChatHistoryMessage[] = [];
        for (const raw of value.messages) {
          const row = readRow(raw);
          if (row) messages.push(row);
        }
        if (!messages.length) return;
        done({
          roomId: typeof value.roomId === 'number' ? value.roomId : 0,
          roomToken,
          messages,
          oldestCursor: value.oldestCursor ?? null,
          savedAt: value.savedAt,
        });
      };
    },
    null,
  );
}

/** Replace a room's cached tail with what a history load just returned. */
export async function writeTranscript(
  userId: string | null,
  entry: {
    roomId: number;
    roomToken: string;
    messages: ChatHistoryMessage[];
    oldestCursor?: { ts: string; id: number } | null;
  },
  now = Date.now(),
): Promise<void> {
  if (!userId || !entry.roomToken) return;
  const messages: ChatHistoryMessage[] = [];
  for (const raw of entry.messages) {
    const row = readRow(raw);
    if (row) messages.push(row);
  }
  const value: CachedTranscript = {
    roomId: entry.roomId,
    roomToken: entry.roomToken,
    messages: boundRows(messages),
    oldestCursor: entry.oldestCursor ?? null,
    savedAt: now,
  };
  await withTx<void>(
    [STORE_TRANSCRIPTS],
    'readwrite',
    (tx) => {
      const store = tx.objectStore(STORE_TRANSCRIPTS);
      const key = transcriptKey(userId, entry.roomToken);
      // Nothing fitting the bounds is a delete rather than an empty record:
      // every read refuses an empty one anyway, so storing it would hold an
      // eviction slot for a room that cannot be painted.
      if (!value.messages.length) {
        store.delete(key);
        return;
      }
      store.put(value, key);
      evictOldestTranscripts(tx);
    },
    undefined,
  );
}

/**
 * Fold streamed rows into a room's cached tail, in one transaction.
 *
 * **A room with nothing cached is left with nothing.** The cache is the tail of
 * the rooms the user actually reads, and a room they have never opened would
 * otherwise acquire a one-message "transcript" that paints offline as though it
 * were the whole conversation — while taking an eviction slot from a room they
 * do open. What this keeps warm is a room already in the cache, which is what a
 * background room needs to still be current when they switch to it offline.
 *
 * Read-modify-write inside a single transaction rather than a read, an await
 * and a write: the stream, a history load and another tab all write here, and
 * two of those interleaving around an await is a lost update.
 */
export async function appendTranscriptRows(
  userId: string | null,
  roomToken: string | null,
  rows: ChatHistoryMessage[],
  now = Date.now(),
): Promise<void> {
  if (!userId || !roomToken || !rows.length) return;
  const incoming: ChatHistoryMessage[] = [];
  for (const raw of rows) {
    const row = readRow(raw);
    if (row) incoming.push(row);
  }
  if (!incoming.length) return;
  await withTx<void>(
    [STORE_TRANSCRIPTS],
    'readwrite',
    (tx) => {
      const store = tx.objectStore(STORE_TRANSCRIPTS);
      const key = transcriptKey(userId, roomToken);
      const req = store.get(key);
      req.onsuccess = () => {
        const value = req.result as CachedTranscript | undefined;
        if (!value || !Array.isArray(value.messages)) return;
        const merged = value.messages.slice();
        for (const row of incoming) {
          const at = merged.findIndex((m) => sameRow(m, row));
          if (at === -1) merged.push(row);
          else merged[at] = row;
        }
        const bounded = boundRows(merged);
        if (!bounded.length) {
          store.delete(key);
          return;
        }
        // `savedAt` moves, which is what keeps a busy room out of the eviction
        // order and out of the TTL: it stamps how fresh the stored rows are,
        // and these rows are from a moment ago.
        store.put({ ...value, messages: bounded, savedAt: now }, key);
        evictOldestTranscripts(tx);
      };
    },
    undefined,
  );
}

/** Forget one room's cached tail. */
export async function deleteTranscript(
  userId: string | null,
  roomToken: string | null,
): Promise<void> {
  if (!userId || !roomToken) return;
  await withTx<void>(
    [STORE_TRANSCRIPTS],
    'readwrite',
    (tx) => {
      tx.objectStore(STORE_TRANSCRIPTS).delete(transcriptKey(userId, roomToken));
    },
    undefined,
  );
}

/**
 * Drop deleted messages from every cached room this user has.
 *
 * A deletion frame names ids and not the room they were in, so this walks the
 * user's own keys rather than being told where to look. Deletions are rare and
 * a user has at most `MAX_CACHED_ROOMS` rooms cached, so the walk is cheaper
 * than carrying a room→message index that every other write would maintain.
 */
export async function removeCachedMessages(userId: string | null, msgIds: number[]): Promise<void> {
  if (!userId || !msgIds.length) return;
  const gone = new Set(msgIds);
  const prefix = `${userId}${KEY_INFIX}`;
  await withTx<void>(
    [STORE_TRANSCRIPTS],
    'readwrite',
    (tx) => {
      const cursoring = tx.objectStore(STORE_TRANSCRIPTS).openCursor();
      cursoring.onsuccess = () => {
        const cursor = cursoring.result;
        if (!cursor) return;
        const key = String(cursor.key);
        const value = cursor.value as CachedTranscript | undefined;
        // The key is rebuilt from the record's own token rather than merely
        // prefix-matched. The infix already makes a collision unreachable for
        // any real user id, so this is the belt to its braces — and it costs a
        // string compare on a walk that runs only when something is deleted.
        if (
          value &&
          typeof value.roomToken === 'string' &&
          key === `${prefix}${value.roomToken}` &&
          Array.isArray(value.messages)
        ) {
          const kept = value.messages.filter(
            (m) => typeof m.msg_id !== 'number' || !gone.has(m.msg_id),
          );
          // Rewriting every room on every deletion would churn the store and
          // move `savedAt` for rooms nothing happened in.
          if (kept.length !== value.messages.length) cursor.update({ ...value, messages: kept });
        }
        cursor.continue();
      };
    },
    undefined,
  );
}

// ---- The room list and the config ----------------------------------------

/**
 * The cached room list, or null.
 *
 * The list is what the sidebar needs to render at all, and offline it is also
 * what the transcript cache is read through — a cached tail is keyed by token,
 * and the token comes from here.
 */
export async function readRooms(
  userId: string | null,
  now = Date.now(),
): Promise<ChatRoom[] | null> {
  if (!userId) return null;
  return withTx<ChatRoom[] | null>(
    [STORE_ROOMS],
    'readonly',
    (tx, done) => {
      const req = tx.objectStore(STORE_ROOMS).get(userId);
      req.onsuccess = () => {
        const value = req.result as Partial<CachedRooms> | undefined;
        if (!value || !Array.isArray(value.rooms) || !value.rooms.length) return;
        if (typeof value.savedAt !== 'number' || now - value.savedAt >= CACHE_TTL_MS) return;
        done(value.rooms as ChatRoom[]);
      };
    },
    null,
  );
}

export async function writeRooms(
  userId: string | null,
  rooms: ChatRoom[],
  now = Date.now(),
): Promise<void> {
  if (!userId) return;
  const value: CachedRooms = { rooms, savedAt: now };
  await withTx<void>(
    [STORE_ROOMS],
    'readwrite',
    (tx) => {
      tx.objectStore(STORE_ROOMS).put(value, userId);
    },
    undefined,
  );
}

/**
 * The cached chat config, or null.
 *
 * Worth keeping because two of its fields decide how the transcript renders
 * before anything is fetched — the external-turn display and the poll cadence —
 * and offline the alternative is a page that reverts to the defaults every time
 * it is remounted.
 */
export async function readConfig(
  userId: string | null,
  now = Date.now(),
): Promise<ChatConfig | null> {
  if (!userId) return null;
  return withTx<ChatConfig | null>(
    [STORE_CONFIG],
    'readonly',
    (tx, done) => {
      const req = tx.objectStore(STORE_CONFIG).get(userId);
      req.onsuccess = () => {
        const value = req.result as Partial<CachedConfig> | undefined;
        if (!value || !value.config || typeof value.config !== 'object') return;
        if (typeof value.savedAt !== 'number' || now - value.savedAt >= CACHE_TTL_MS) return;
        done(value.config as ChatConfig);
      };
    },
    null,
  );
}

export async function writeConfig(
  userId: string | null,
  config: ChatConfig,
  now = Date.now(),
): Promise<void> {
  if (!userId) return;
  const value: CachedConfig = { config, savedAt: now };
  await withTx<void>(
    [STORE_CONFIG],
    'readwrite',
    (tx) => {
      tx.objectStore(STORE_CONFIG).put(value, userId);
    },
    undefined,
  );
}

/**
 * The signed-in user, or null.
 *
 * The one cached value the *root layout* reads (ISSUE-354). Everything else in
 * this file is read by the chat store, under a page the layout renders only
 * once `getMe()` has answered — so with no connection none of it was reachable,
 * and the whole offline feature failed one layer above itself.
 *
 * What is stored decides what the UI shows and grants nothing: every
 * authorization check is server-side, and a request made against a stale
 * identity still 401s. The failure mode of a wrong answer here is a wrong name
 * in a header offline, not access.
 *
 * **Validated to the depth the layout dereferences**, the way `readRow` is and
 * for the same reason — a record written by a build whose shape has since
 * changed. `features` is the field that matters: the nav reads six of its
 * members, and a record without it throws inside the template rather than in
 * this file's caller, so the layout renders *nothing* instead of the error
 * page this whole change exists to remove, on every launch until the TTL runs
 * out. A record that cannot be rendered has to read as an empty cache.
 */
export async function readUser(userId: string | null, now = Date.now()): Promise<User | null> {
  if (!userId) return null;
  return withTx<User | null>(
    [STORE_CONFIG],
    'readonly',
    (tx, done) => {
      const req = tx.objectStore(STORE_CONFIG).get(userKey(userId));
      req.onsuccess = () => {
        const value = req.result as Partial<CachedUser> | undefined;
        if (!value || !value.user || typeof value.user !== 'object') return;
        const cached = value.user as Partial<User>;
        if (typeof cached.username !== 'string' || !cached.username) return;
        if (!cached.features || typeof cached.features !== 'object') return;
        if (typeof value.savedAt !== 'number' || now - value.savedAt >= CACHE_TTL_MS) return;
        done(value.user as User);
      };
    },
    null,
  );
}

export async function writeUser(
  userId: string | null,
  user: User,
  now = Date.now(),
): Promise<void> {
  if (!userId) return;
  const value: CachedUser = { user, savedAt: now };
  await withTx<void>(
    [STORE_CONFIG],
    'readwrite',
    (tx) => {
      tx.objectStore(STORE_CONFIG).put(value, userKey(userId));
    },
    undefined,
  );
}

// ---- Attachment bytes -----------------------------------------------------

/**
 * Whether `extraBytes` would still leave the origin room to breathe.
 *
 * Asked before a blob write, and answered `true` when there is nothing to ask:
 * a browser with no `navigator.storage.estimate`, or one that reports figures
 * that make no sense, must not become a client-side refusal of a file that
 * would have been stored perfectly well. Same discipline as the composer's
 * server-limit check, which lets the server decide when `/chat/config` never
 * answered.
 *
 * The estimate is the whole origin's, not this database's, so it counts the
 * send queue and the transcript cache too — which is the right budget, because
 * eviction takes all three together.
 */
export async function hasHeadroom(extraBytes: number): Promise<boolean> {
  try {
    const storage = typeof navigator === 'undefined' ? undefined : navigator.storage;
    if (!storage?.estimate) return true;
    const { usage, quota } = await storage.estimate();
    if (typeof usage !== 'number' || typeof quota !== 'number' || quota <= 0) return true;
    return usage + extraBytes <= quota * STORAGE_HEADROOM_FRACTION;
  } catch {
    return true;
  }
}

/**
 * Hold one file's bytes under `blobId`, or refuse.
 *
 * Returns whether the bytes are stored, and `false` is the composer's cue to
 * say so: past any of the three bounds the file is refused with a sentence and
 * nothing is written. A `false` from an unusable database reads the same way,
 * which is the point — a caller has one question to ask and one answer to
 * handle.
 *
 * The running total is summed inside the same transaction as the write, so two
 * files picked at once cannot both read a total that leaves room and then both
 * store. It walks every blob rather than a per-user subtotal because the bound
 * is on the origin's storage, which is what the origin is evicted for.
 */
export async function putBlob(
  blobId: string,
  bytes: ArrayBuffer,
  meta: { name: string; mimeType: string; size: number },
  now = Date.now(),
): Promise<boolean> {
  if (!blobId) return false;
  if (meta.size > MAX_PENDING_BLOB_BYTES) return false;
  if (!(await hasHeadroom(meta.size))) return false;
  return withTx<boolean>(
    [STORE_BLOBS],
    'readwrite',
    (tx, done) => {
      const store = tx.objectStore(STORE_BLOBS);
      const cursoring = store.openCursor();
      let total = 0;
      cursoring.onsuccess = () => {
        const cursor = cursoring.result;
        if (cursor) {
          const value = cursor.value as Partial<StoredBlob> | undefined;
          // A record whose size is unreadable is counted at nothing rather
          // than skipping the bound entirely; `pruneOffline` collects it.
          if (typeof value?.size === 'number') total += value.size;
          cursor.continue();
          return;
        }
        // Over the shared bound: nothing is written and the transaction is
        // left to complete, so `value` stays `false` and the caller is told.
        if (total + meta.size > MAX_PENDING_BLOB_TOTAL) return;
        const record: StoredBlob = {
          bytes,
          name: meta.name,
          mimeType: meta.mimeType,
          size: meta.size,
          createdAt: now,
        };
        store.put(record, blobId);
        done(true);
      };
    },
    false,
  );
}

/**
 * The bytes a stored record carries, or null when it carries none.
 *
 * Branded rather than `instanceof`, because a structured clone can hand back a
 * buffer belonging to a different realm — which `fake-indexeddb` does — and an
 * `instanceof` against the wrong realm's constructor rejects a perfectly good
 * one. The brand is what the value *is*; a plain object left by a build whose
 * shape has since changed reads `[object Object]` and is still refused.
 *
 * A view over a buffer counts and is normalized to the buffer behind it. The
 * write side always stores an `ArrayBuffer`, so this should not arise — but
 * "not bytes" is destructive here, failing the message and dropping its other
 * files with it, and accepting a view costs one line against an engine or
 * polyfill in the stack handing one back.
 */
function readBytes(value: unknown): ArrayBuffer | null {
  if (Object.prototype.toString.call(value) === '[object ArrayBuffer]') return value as ArrayBuffer;
  if (ArrayBuffer.isView(value)) return value.buffer as ArrayBuffer;
  return null;
}

/** One held file, or null when it is gone or was never buffer-shaped. */
export async function getBlob(blobId: string): Promise<StoredBlob | null> {
  if (!blobId) return null;
  return withTx<StoredBlob | null>(
    [STORE_BLOBS],
    'readonly',
    (tx, done) => {
      const req = tx.objectStore(STORE_BLOBS).get(blobId);
      req.onsuccess = () => {
        const value = req.result as Partial<StoredBlob> | undefined;
        // Type-checked rather than truthiness-checked: this is handed straight
        // to an upload, and a record left half-written by a build whose shape
        // has since changed would be POSTed as `[object Object]`.
        const bytes = value ? readBytes(value.bytes) : null;
        if (!value || !bytes) return;
        done({
          bytes,
          name: typeof value.name === 'string' ? value.name : 'attachment',
          mimeType: typeof value.mimeType === 'string' ? value.mimeType : '',
          size: typeof value.size === 'number' ? value.size : bytes.byteLength,
          createdAt: typeof value.createdAt === 'number' ? value.createdAt : 0,
        });
      };
    },
    null,
  );
}

/** Forget one held file. Called as soon as its upload has landed. */
export async function deleteBlob(blobId: string): Promise<void> {
  if (!blobId) return;
  await withTx<void>(
    [STORE_BLOBS],
    'readwrite',
    (tx) => {
      tx.objectStore(STORE_BLOBS).delete(blobId);
    },
    undefined,
  );
}

/** Every held file's id, for a caller reconciling them against its own list. */
export async function listBlobIds(): Promise<string[]> {
  return withTx<string[]>(
    [STORE_BLOBS],
    'readonly',
    (tx, done) => {
      const req = tx.objectStore(STORE_BLOBS).getAllKeys();
      req.onsuccess = () => done(req.result.map((k) => String(k)));
    },
    [],
  );
}

// ---- Housekeeping ---------------------------------------------------------

/**
 * Drop everything past `CACHE_TTL_MS`, across every user on this profile.
 *
 * Called once at startup rather than on a timer: the cost of an expired entry
 * is storage, not correctness — every read refuses one anyway — so collecting
 * it when the app opens is soon enough, and it is the one moment where doing
 * work nobody is waiting on is free.
 *
 * Blobs are collected by a different rule and only when the caller can state
 * it: `referencedBlobIds` is every blob some live queue entry still names, and
 * anything outside it *and* older than `BLOB_GC_MIN_AGE_MS` is dead storage.
 * Omit the argument and no blob is touched — a caller that cannot enumerate
 * the references must not be able to delete bytes it cannot account for, and
 * an empty set is a legitimate answer that means something else entirely.
 */
export async function pruneOffline(
  now = Date.now(),
  referencedBlobIds?: ReadonlySet<string> | null,
): Promise<void> {
  const cutoff = now - CACHE_TTL_MS;
  for (const name of [STORE_TRANSCRIPTS, STORE_ROOMS, STORE_CONFIG]) {
    await withTx<void>(
      [name],
      'readwrite',
      (tx) => {
        const store = tx.objectStore(name);
        // The store rather than the `savedAt` index, though the transcripts
        // store has one: a record whose stamp is missing or not a number has
        // no index entry at all, and it is exactly the record worth collecting
        // — every read already refuses it, so it is dead storage holding one
        // of `MAX_CACHED_ROOMS` slots.
        const cursoring = store.openCursor();
        cursoring.onsuccess = () => {
          const cursor = cursoring.result;
          if (!cursor) return;
          const value = cursor.value as { savedAt?: number } | undefined;
          if (typeof value?.savedAt !== 'number' || value.savedAt <= cutoff) cursor.delete();
          cursor.continue();
        };
      },
      undefined,
    );
  }
  if (!referencedBlobIds) return;
  const blobCutoff = now - BLOB_GC_MIN_AGE_MS;
  await withTx<void>(
    [STORE_BLOBS],
    'readwrite',
    (tx) => {
      const cursoring = tx.objectStore(STORE_BLOBS).openCursor();
      cursoring.onsuccess = () => {
        const cursor = cursoring.result;
        if (!cursor) return;
        const value = cursor.value as Partial<StoredBlob> | undefined;
        // No usable stamp reads as ancient, which is what a record nothing
        // references and nothing can date actually is.
        const createdAt = typeof value?.createdAt === 'number' ? value.createdAt : 0;
        if (!referencedBlobIds.has(String(cursor.key)) && createdAt <= blobCutoff) cursor.delete();
        cursor.continue();
      };
    },
    undefined,
  );
}

/**
 * Empty every store, for every user on this profile.
 *
 * One caller, and it is the settings row "Clear offline data" (ISSUE-202),
 * which unregisters the service worker and drops its caches alongside this. It
 * is the escape hatch for storage that has gone wrong in a way nothing else
 * explains — a document that will not update, a transcript that will not
 * reconcile — so it clears the whole database rather than this user's share of
 * it. On the shell those are the same thing; on a shared profile, someone
 * asking for the offline data to be cleared is not asking for another
 * account's to be kept.
 *
 * `clear()` per store rather than `deleteDatabase`: this module memoizes an
 * open connection, and a delete would sit blocked behind it (and behind any
 * other tab's) instead of failing — the button would do nothing while the
 * caller reloaded over it. An emptied store reads exactly as a missing one
 * everywhere above.
 *
 * One transaction across all four, so this cannot half-succeed and leave the
 * held bytes of a queue entry behind without the transcript they belong to.
 *
 * The only function here that **reports** whether it worked, against the
 * module's own resolve-rather-than-reject discipline. Everywhere else a
 * storage failure costs the offline paint and the caller has nothing to say
 * about it; here the caller is a user who pressed a button to escape a state
 * they can see, and reloading over a clear that quietly did nothing is the one
 * outcome the row must not produce. `false` means the transaction did not
 * complete; a database that cannot be opened at all is `true`, since there is
 * nothing stored to clear.
 */
export async function clearOffline(): Promise<boolean> {
  const db = await openOfflineDb();
  if (!db) return true;
  const stores = [STORE_TRANSCRIPTS, STORE_ROOMS, STORE_CONFIG, STORE_BLOBS];
  return withTx<boolean>(
    stores,
    'readwrite',
    (tx, done) => {
      for (const name of stores) tx.objectStore(name).clear();
      // Reported through `done`, so it is `oncomplete` that answers rather
      // than the last `clear()` having been issued.
      done(true);
    },
    false,
  );
}
