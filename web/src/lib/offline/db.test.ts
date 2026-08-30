/**
 * The offline read cache's storage layer (ISSUE-202).
 *
 * Covers the three bounds a room is written under, the eviction order across
 * rooms, expiry on both the read and the prune, the stream-frame merge, and —
 * the property every caller depends on and none of them checks — that a
 * database which cannot be opened at all reads as an empty cache rather than
 * as an error.
 *
 * `fake-indexeddb` because jsdom ships no IndexedDB. It is the reference
 * implementation of the same spec, so what is exercised here is the wrapper's
 * transaction discipline rather than a mock of it.
 */
import 'fake-indexeddb/auto';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatHistoryMessage, ChatRoom } from '$lib/api';

const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;

function roomRecord(token: string): ChatRoom {
  return {
    id: 1,
    token,
    name: 'General',
    archived: false,
    created_at: '',
    updated_at: '',
  };
}

function row(n: number, text = `m${n}`): ChatHistoryMessage {
  return {
    role: n % 2 === 0 ? 'assistant' : 'user',
    text,
    msg_id: n,
    created_at: '2026-06-10T12:00:00Z',
  };
}

/** A fresh module graph over a fresh database, so no test inherits another's. */
async function freshDb() {
  // A new factory rather than `deleteDatabase`: the module memoizes its open
  // connection, and a deleted database behind a live handle is a different
  // state from a new origin.
  globalThis.indexedDB = new IDBFactory();
  vi.resetModules();
  return import('./db');
}

describe('offline cache — transcripts', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('round-trips a transcript and trims it to the per-room message cap', async () => {
    const db = await freshDb();
    const many = Array.from({ length: db.CACHE_MESSAGES_PER_ROOM + 20 }, (_, i) => row(i + 1));
    await db.writeTranscript('alice', {
      roomId: 7,
      roomToken: 'tok7',
      messages: many,
      oldestCursor: { ts: '2026-06-10 11:00:00', id: 3 },
    });

    const back = await db.readTranscript('alice', 'tok7');
    expect(back?.messages).toHaveLength(db.CACHE_MESSAGES_PER_ROOM);
    // The *tail* survives: the newest is what opening the room shows.
    expect(back?.messages.at(-1)?.msg_id).toBe(many.at(-1)?.msg_id);
    expect(back?.messages[0].msg_id).toBe(21);
    expect(back?.oldestCursor).toEqual({ ts: '2026-06-10 11:00:00', id: 3 });
    expect(back?.roomId).toBe(7);
  });

  it('namespaces by user, so one profile does not read another user out', async () => {
    const db = await freshDb();
    await db.writeTranscript('alice', { roomId: 1, roomToken: 'shared', messages: [row(1)] });
    expect(await db.readTranscript('bob', 'shared')).toBeNull();
    expect(await db.readTranscript('alice', 'shared')).not.toBeNull();
  });

  it('drops the oldest rows when a room exceeds the byte bound', async () => {
    const db = await freshDb();
    // Three rows at two fifths of the budget each: two fit, three do not, so
    // exactly the oldest goes.
    const fat = (n: number) => row(n, 'x'.repeat(Math.floor(db.MAX_ROOM_CACHE_BYTES * 0.4)));
    await db.writeTranscript('alice', {
      roomId: 1,
      roomToken: 'fat',
      messages: [fat(1), fat(2), fat(3)],
    });

    const back = await db.readTranscript('alice', 'fat');
    expect(back?.messages.map((m) => m.msg_id)).toEqual([2, 3]);
  });

  it('evicts the least recently saved room past the room cap', async () => {
    const db = await freshDb();
    for (let i = 0; i < db.MAX_CACHED_ROOMS; i++) {
      await db.writeTranscript(
        'alice',
        { roomId: i, roomToken: `t${i}`, messages: [row(i + 1)] },
        1000 + i,
      );
    }
    expect(await db.readTranscript('alice', 't0', 1000)).not.toBeNull();

    await db.writeTranscript(
      'alice',
      { roomId: 99, roomToken: 'newest', messages: [row(500)] },
      9000,
    );

    expect(await db.readTranscript('alice', 't0', 9000)).toBeNull();
    expect(await db.readTranscript('alice', 't1', 9000)).not.toBeNull();
    expect(await db.readTranscript('alice', 'newest', 9000)).not.toBeNull();
  });

  it('refuses a transcript older than the TTL without waiting for the prune', async () => {
    const db = await freshDb();
    const written = 1_000_000;
    await db.writeTranscript('alice', { roomId: 1, roomToken: 'old', messages: [row(1)] }, written);

    expect(await db.readTranscript('alice', 'old', written + db.CACHE_TTL_MS - 1)).not.toBeNull();
    expect(await db.readTranscript('alice', 'old', written + db.CACHE_TTL_MS)).toBeNull();
  });

  it('deletes a room transcript', async () => {
    const db = await freshDb();
    await db.writeTranscript('alice', { roomId: 1, roomToken: 'gone', messages: [row(1)] });
    await db.deleteTranscript('alice', 'gone');
    expect(await db.readTranscript('alice', 'gone')).toBeNull();
  });
});

describe('offline cache — streamed rows', () => {
  it('appends new rows and replaces a turn the stream re-sends', async () => {
    const db = await freshDb();
    const running: ChatHistoryMessage = {
      role: 'assistant',
      text: '',
      task_id: 12,
      status: 'running',
      created_at: '2026-06-10T12:00:00Z',
    };
    await db.writeTranscript('alice', { roomId: 1, roomToken: 'r', messages: [row(1), running] });

    const finished = { ...running, text: 'done', status: 'completed' };
    await db.appendTranscriptRows('alice', 'r', [finished, row(3)]);

    const back = await db.readTranscript('alice', 'r');
    expect(back?.messages).toHaveLength(3);
    expect(back?.messages[1].text).toBe('done');
    expect(back?.messages[2].msg_id).toBe(3);
  });

  it('leaves a room with nothing cached with nothing cached', async () => {
    const db = await freshDb();
    await db.appendTranscriptRows('alice', 'never-opened', [row(1)]);
    expect(await db.readTranscript('alice', 'never-opened')).toBeNull();
  });

  it('holds the per-room cap while appending', async () => {
    const db = await freshDb();
    await db.writeTranscript('alice', { roomId: 1, roomToken: 'r', messages: [row(1)] });
    const more = Array.from({ length: db.CACHE_MESSAGES_PER_ROOM + 5 }, (_, i) => row(i + 2));
    await db.appendTranscriptRows('alice', 'r', more);

    const back = await db.readTranscript('alice', 'r');
    expect(back?.messages).toHaveLength(db.CACHE_MESSAGES_PER_ROOM);
    expect(back?.messages.at(-1)?.msg_id).toBe(more.at(-1)?.msg_id);
  });

  it('removes deleted messages from every cached room of that user', async () => {
    const db = await freshDb();
    await db.writeTranscript('alice', { roomId: 1, roomToken: 'a', messages: [row(1), row(2)] });
    await db.writeTranscript('alice', { roomId: 2, roomToken: 'b', messages: [row(3), row(4)] });
    await db.writeTranscript('bob', { roomId: 3, roomToken: 'c', messages: [row(2)] });

    await db.removeCachedMessages('alice', [2, 3]);

    expect((await db.readTranscript('alice', 'a'))?.messages.map((m) => m.msg_id)).toEqual([1]);
    expect((await db.readTranscript('alice', 'b'))?.messages.map((m) => m.msg_id)).toEqual([4]);
    // Another user's copy of the same id is not this deletion's business.
    expect((await db.readTranscript('bob', 'c'))?.messages.map((m) => m.msg_id)).toEqual([2]);
  });
});

describe('offline cache — rooms, config and pruning', () => {
  it('round-trips the room list and the config', async () => {
    const db = await freshDb();
    const rooms = [roomRecord('t1')];
    await db.writeRooms('alice', rooms);
    await db.writeConfig('alice', {
      max_prompt_chars: 10,
      max_attachment_mb: 5,
      attachment_extensions: ['pdf'],
      client_poll_interval_ms: 1500,
      user_id: 'alice',
    });

    expect(await db.readRooms('alice')).toEqual(rooms);
    expect((await db.readConfig('alice'))?.client_poll_interval_ms).toBe(1500);
    expect(await db.readRooms('bob')).toBeNull();
  });

  it('prunes everything past the TTL and leaves the rest', async () => {
    const db = await freshDb();
    const old = 1_000_000;
    const fresh = old + 29 * DAY;
    await db.writeTranscript('alice', { roomId: 1, roomToken: 'old', messages: [row(1)] }, old);
    await db.writeTranscript('alice', { roomId: 2, roomToken: 'new', messages: [row(2)] }, fresh);
    // A non-empty list, because `readRooms` refuses an empty one on its own —
    // asserting on `[]` would pass with the prune deleted entirely.
    await db.writeRooms('alice', [roomRecord('t1')], old);
    await db.writeConfig(
      'alice',
      {
        max_prompt_chars: 1,
        max_attachment_mb: 1,
        attachment_extensions: [],
        client_poll_interval_ms: 1,
      },
      fresh,
    );

    await db.pruneOffline(old + db.CACHE_TTL_MS + 1);

    // Read at the *written* stamps so the assertion is about the prune rather
    // than about the reader's own expiry check.
    expect(await db.readTranscript('alice', 'old', old)).toBeNull();
    expect(await db.readTranscript('alice', 'new', fresh)).not.toBeNull();
    expect(await db.readRooms('alice', old)).toBeNull();
    expect(await db.readConfig('alice', fresh)).not.toBeNull();
  });

  it('collects a record whose stamp the index cannot see, and evicts nothing for it', async () => {
    const db = await freshDb();
    // Written behind the module's back, the way a build with a different value
    // shape would have left it: no usable `savedAt`, so it has no entry in the
    // `savedAt` index and no read will ever return it.
    const handle = await db.openOfflineDb();
    await new Promise<void>((resolve, reject) => {
      const tx = handle!.transaction([db.STORE_TRANSCRIPTS], 'readwrite');
      tx.objectStore(db.STORE_TRANSCRIPTS).put(
        { roomId: 1, roomToken: 'stamped-wrong', messages: [row(1)] },
        'alice:stamped-wrong',
      );
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });

    // It must not count against the room cap: filling the cache to the cap
    // alongside it evicts nothing, where a store-wide count would have thrown
    // out a live room to make room for a dead record.
    for (let i = 0; i < db.MAX_CACHED_ROOMS; i++) {
      await db.writeTranscript(
        'alice',
        { roomId: i, roomToken: `t${i}`, messages: [row(i + 1)] },
        2000 + i,
      );
    }
    expect(await db.readTranscript('alice', 't0', 2000)).not.toBeNull();

    await db.pruneOffline(9_000_000);
    const survivors = await new Promise<IDBValidKey[]>((resolve, reject) => {
      const tx = handle!.transaction([db.STORE_TRANSCRIPTS], 'readonly');
      const req = tx.objectStore(db.STORE_TRANSCRIPTS).getAllKeys();
      req.onsuccess = () => resolve(req.result);
      tx.onerror = () => reject(tx.error);
    });
    expect(survivors).not.toContain('alice:stamped-wrong');
  });

  it('does not let one user id run into another across the key separator', async () => {
    const db = await freshDb();
    // `${userId}:${roomToken}` has no infix, so `a` + `b:tok` and `a:b` + `tok`
    // spell the same key. A deletion for `a` must not reach into `a:b`'s room.
    await db.writeTranscript('a', { roomId: 1, roomToken: 'b:tok', messages: [row(1)] });
    await db.writeTranscript('a:b', { roomId: 2, roomToken: 'tok', messages: [row(1)] });

    await db.removeCachedMessages('a', [1]);

    expect(await db.readTranscript('a', 'b:tok')).toBeNull();
    expect((await db.readTranscript('a:b', 'tok'))?.messages.map((m) => m.msg_id)).toEqual([1]);
  });
});

describe('offline cache — when there is no database', () => {
  afterEach(() => {
    // A test that trips its own budget never reaches its cleanup, and a fake
    // clock left running takes every later file's timers with it.
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('reads empty and writes nothing when opening throws', async () => {
    vi.stubGlobal('indexedDB', {
      open: () => {
        throw new DOMException('denied', 'SecurityError');
      },
    });
    vi.resetModules();
    const db = await import('./db');

    await expect(db.openOfflineDb()).resolves.toBeNull();
    await expect(
      db.writeTranscript('alice', { roomId: 1, roomToken: 't', messages: [row(1)] }),
    ).resolves.toBeUndefined();
    await expect(db.readTranscript('alice', 't')).resolves.toBeNull();
    await expect(db.appendTranscriptRows('alice', 't', [row(2)])).resolves.toBeUndefined();
    await expect(db.deleteTranscript('alice', 't')).resolves.toBeUndefined();
    await expect(db.removeCachedMessages('alice', [1])).resolves.toBeUndefined();
    await expect(db.readRooms('alice')).resolves.toBeNull();
    await expect(db.writeRooms('alice', [])).resolves.toBeUndefined();
    await expect(db.readConfig('alice')).resolves.toBeNull();
    await expect(db.pruneOffline()).resolves.toBeUndefined();

    vi.unstubAllGlobals();
  });

  it('reads empty when the open request fails', async () => {
    const request: Record<string, unknown> = {};
    vi.stubGlobal('indexedDB', {
      open: () => {
        // The failure arrives after the caller has attached its handlers,
        // which is the shape a real quota or corruption refusal has.
        setTimeout(() => (request.onerror as () => void)?.(), 0);
        return request;
      },
    });
    vi.resetModules();
    const db = await import('./db');

    await expect(db.readTranscript('alice', 't')).resolves.toBeNull();
    vi.unstubAllGlobals();
  });

  it('reads empty when the open request never fires an event at all', async () => {
    // The failure the swallow-everything discipline does not otherwise cover,
    // and the one WebKit actually produces: no error, no success, nothing. An
    // unbounded wait here hangs `init()`'s awaited cache read and leaves the
    // chat page on its loading state for the life of the session.
    vi.useFakeTimers();
    vi.stubGlobal('indexedDB', { open: () => ({}) });
    vi.resetModules();
    const db = await import('./db');

    const reading = db.readTranscript('alice', 't');
    await vi.advanceTimersByTimeAsync(db.OFFLINE_DB_TIMEOUT_MS + 1);
    await expect(reading).resolves.toBeNull();

    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('reads empty when a transaction never completes', async () => {
    const db = await freshDb();
    await db.writeTranscript('alice', { roomId: 1, roomToken: 't', messages: [row(1)] });
    // Fake timers only from here: `fake-indexeddb` schedules its own callbacks
    // on the timer queue, so a real write cannot complete under them.
    vi.useFakeTimers();
    // A live connection whose transactions go nowhere — storage wedged under
    // pressure, rather than refusing outright.
    const handle = (await db.openOfflineDb())!;
    handle.transaction = (() => ({
      objectStore: () => ({ get: () => ({}), put: () => ({}), delete: () => ({}) }),
    })) as unknown as IDBDatabase['transaction'];

    const reading = db.readTranscript('alice', 't');
    await vi.advanceTimersByTimeAsync(db.OFFLINE_DB_TIMEOUT_MS + 1);
    await expect(reading).resolves.toBeNull();

    vi.useRealTimers();
  });

  it('reads empty when the origin has no IndexedDB at all', async () => {
    vi.stubGlobal('indexedDB', undefined);
    vi.resetModules();
    const db = await import('./db');

    await expect(db.readRooms('alice')).resolves.toBeNull();
    await expect(db.writeRooms('alice', [])).resolves.toBeUndefined();
    vi.unstubAllGlobals();
  });

  it('does nothing at all without a user id', async () => {
    const db = await freshDb();
    await db.writeTranscript(null, { roomId: 1, roomToken: 't', messages: [row(1)] });
    expect(await db.readTranscript(null, 't')).toBeNull();
    // Nothing was written under any key by the null-user call above.
    expect(await db.readTranscript('alice', 't')).toBeNull();
  });
});
