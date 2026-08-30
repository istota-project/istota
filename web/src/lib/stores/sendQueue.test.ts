/**
 * The send queue's persistence layer (ISSUE-238).
 *
 * Mirrors `drafts.test.ts`, because the module mirrors `drafts.ts`. What is
 * different is what a bound costs: a clamped draft is half a thought cut
 * short, while a clamped or evicted queue entry is a message the user has
 * committed to sending. So the tests below are as interested in what survives
 * an eviction as in what goes.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { ChatAttachment } from '$lib/api';
import {
  readQueue,
  writeQueue,
  dropQueue,
  readAllQueues,
  SEND_QUEUE_STORAGE_KEY,
  QUEUE_TTL_MS,
  MAX_QUEUED_PER_ROOM,
  MAX_QUEUE_ROOMS,
  MAX_QUEUE_CHARS,
  MAX_QUEUE_TOTAL_CHARS,
  type StoredQueuedSend,
} from './sendQueue';

const DAY = 24 * 60 * 60 * 1000;

function entry(text: string, over: Partial<StoredQueuedSend> = {}): StoredQueuedSend {
  return {
    cid: 1,
    text,
    attachments: [],
    held: false,
    queuedAt: Date.now(),
    reason: 'busy',
    ...over,
  };
}

function attachment(name = 'spec.pdf'): ChatAttachment {
  return { path: `/host/inbox/${name}`, name, size: 12, workspace_path: `/Users/u/inbox/${name}` };
}

function stored(): Record<string, StoredQueuedSend[]> {
  return JSON.parse(localStorage.getItem(SEND_QUEUE_STORAGE_KEY) ?? '{}');
}

function seed(map: Record<string, unknown>) {
  localStorage.setItem(SEND_QUEUE_STORAGE_KEY, JSON.stringify(map));
}

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-08-29T12:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('sendQueue', () => {
  it('round-trips a queue under its key', () => {
    writeQueue('u:room:t1', [entry('first'), entry('second', { cid: 2 })]);
    expect(readQueue('u:room:t1').map((e) => e.text)).toEqual(['first', 'second']);
  });

  it('carries every field a send is rebuilt from', () => {
    writeQueue('u:room:t1', [
      entry('have a look', {
        cid: 9,
        attachments: [attachment()],
        replyTo: { msgId: 7, role: 'assistant', excerpt: 'earlier' },
        replyToMsgId: 7,
        idempotencyKey: 'abc-123',
        held: true,
        queuedAt: Date.now() - 1000,
      }),
    ]);
    const [back] = readQueue('u:room:t1');
    expect(back.cid).toBe(9);
    expect(back.attachments).toEqual([attachment()]);
    expect(back.replyTo).toEqual({ msgId: 7, role: 'assistant', excerpt: 'earlier' });
    expect(back.replyToMsgId).toBe(7);
    expect(back.idempotencyKey).toBe('abc-123');
    expect(back.held).toBe(true);
    expect(back.queuedAt).toBe(Date.now() - 1000);
  });

  it('reads an entry written before the reason existed as a busy one', () => {
    // Defaulting the other way would take every entry stored by the build
    // before this one and send it unasked on the next load (ISSUE-202).
    seed({
      'u:room:t1': [
        { cid: 1, text: 'from the old build', attachments: [], held: false, queuedAt: Date.now() },
        {
          cid: 2,
          text: 'nonsense reason',
          attachments: [],
          held: false,
          queuedAt: Date.now(),
          reason: 'whenever',
        },
      ],
    });
    expect(readQueue('u:room:t1').map((e) => e.reason)).toEqual(['busy', 'busy']);
  });

  it('round-trips an offline entry as one', () => {
    writeQueue('u:room:t1', [entry('written in a lift', { reason: 'offline' })]);
    expect(readQueue('u:room:t1')[0].reason).toBe('offline');
  });

  it('keeps each room separate', () => {
    writeQueue('u:room:t1', [entry('for A')]);
    writeQueue('u:room:t2', [entry('for B')]);
    expect(readQueue('u:room:t1').map((e) => e.text)).toEqual(['for A']);
    expect(readQueue('u:room:t2').map((e) => e.text)).toEqual(['for B']);
  });

  it('returns an empty list for a key nothing was written under', () => {
    expect(readQueue('u:room:nope')).toEqual([]);
  });

  it('writing an empty list drops the room, leaving the others', () => {
    writeQueue('u:room:t1', [entry('drained away')]);
    writeQueue('u:room:t2', [entry('still waiting')]);
    expect(writeQueue('u:room:t1', [])).toEqual([]);
    expect(stored()['u:room:t1']).toBeUndefined();
    expect(readQueue('u:room:t2').map((e) => e.text)).toEqual(['still waiting']);
  });

  it('does not touch storage when emptying a room that has no queue', () => {
    writeQueue('u:room:t1', [entry('kept')]);
    const before = localStorage.getItem(SEND_QUEUE_STORAGE_KEY);
    writeQueue('u:room:t9', []);
    expect(localStorage.getItem(SEND_QUEUE_STORAGE_KEY)).toBe(before);
  });

  it('returns what was actually stored', () => {
    // `writeDraft`'s contract, for the same reason: a caller comparing its own
    // copy against the stored one must be able to hold the stored one.
    const written = writeQueue('u:room:t1', [entry('x'.repeat(MAX_QUEUE_CHARS + 100))]);
    expect(written).toHaveLength(1);
    expect(written[0].text.length).toBe(MAX_QUEUE_CHARS);
    expect(written[0].text).toBe(readQueue('u:room:t1')[0].text);
  });

  it('readAllQueues hands back every room at once', () => {
    writeQueue('u:room:t1', [entry('for A')]);
    writeQueue('u:room:t2', [entry('for B'), entry('and again', { cid: 2 })]);
    const all = readAllQueues();
    expect(Object.keys(all).sort()).toEqual(['u:room:t1', 'u:room:t2']);
    expect(all['u:room:t2']).toHaveLength(2);
  });

  it('dropQueue removes one room and leaves the others', () => {
    writeQueue('u:room:t1', [entry('for A')]);
    writeQueue('u:room:t2', [entry('for B')]);
    dropQueue('u:room:t1');
    expect(readQueue('u:room:t1')).toEqual([]);
    expect(readQueue('u:room:t2').map((e) => e.text)).toEqual(['for B']);
  });

  it('dropQueue does not touch storage for a room that has none', () => {
    writeQueue('u:room:t2', [entry('for B')]);
    const before = localStorage.getItem(SEND_QUEUE_STORAGE_KEY);
    dropQueue('u:room:t404');
    expect(localStorage.getItem(SEND_QUEUE_STORAGE_KEY)).toBe(before);
  });
});

describe('sendQueue — expiry', () => {
  it('does not restore an entry older than the TTL', () => {
    writeQueue('u:room:t1', [entry('stale')]);
    vi.setSystemTime(Date.now() + QUEUE_TTL_MS + 1);
    expect(readQueue('u:room:t1')).toEqual([]);
  });

  it('keeps an entry that is old but still inside the TTL', () => {
    writeQueue('u:room:t1', [entry('recent enough')]);
    vi.setSystemTime(Date.now() + QUEUE_TTL_MS - DAY);
    expect(readQueue('u:room:t1').map((e) => e.text)).toEqual(['recent enough']);
  });

  it('expires entry by entry, not room by room', () => {
    // The head of a room's queue can age out while a later message is fresh —
    // the queue only drains when the user comes back to the room.
    seed({
      'u:room:t1': [
        entry('written last week', { queuedAt: Date.now() - QUEUE_TTL_MS - 1 }),
        entry('written just now', { cid: 2 }),
      ],
    });
    expect(readQueue('u:room:t1').map((e) => e.text)).toEqual(['written just now']);
  });

  it('drops expired entries on the next write', () => {
    writeQueue('u:room:t1', [entry('stale')]);
    vi.setSystemTime(Date.now() + QUEUE_TTL_MS + 1);
    writeQueue('u:room:t2', [entry('fresh')]);
    expect(stored()['u:room:t1']).toBeUndefined();
  });

  it('is shorter than a draft would get', async () => {
    // A queued message is a pending action, not half a thought, so it must not
    // still be waiting to fire a month later.
    const { DRAFT_TTL_MS } = await import('./drafts');
    expect(QUEUE_TTL_MS).toBeLessThan(DRAFT_TTL_MS);
  });
});

describe('sendQueue — malformed storage', () => {
  it('survives a corrupt payload', () => {
    localStorage.setItem(SEND_QUEUE_STORAGE_KEY, 'not json');
    expect(readQueue('u:room:t1')).toEqual([]);
    writeQueue('u:room:t1', [entry('recovered')]);
    expect(readQueue('u:room:t1').map((e) => e.text)).toEqual(['recovered']);
  });

  it('ignores a payload that is an array rather than a map', () => {
    seed(['nope'] as unknown as Record<string, unknown>);
    expect(readQueue('u:room:t1')).toEqual([]);
  });

  it('ignores a room whose value is not a list', () => {
    seed({ 'u:room:t1': { text: 'not a queue' } });
    expect(readQueue('u:room:t1')).toEqual([]);
  });

  it('discards an entry that is not send-shaped, keeping its neighbours', () => {
    seed({
      'u:room:t1': [
        { cid: 'one', text: 'bad cid', attachments: [], held: false, queuedAt: Date.now() },
        { cid: 2, text: 42, attachments: [], held: false, queuedAt: Date.now() },
        { cid: 3, text: 'no timestamp', attachments: [], held: false },
        { cid: 4, text: 'no attachments list', held: false, queuedAt: Date.now() },
        null,
        entry('good', { cid: 6 }),
      ],
    });
    expect(readQueue('u:room:t1').map((e) => e.text)).toEqual(['good']);
  });

  it('discards an entry whose timestamp did not survive serialization', () => {
    // JSON writes NaN and Infinity as null and refuses to parse either
    // literal, so null is the only shape that can reach the reader.
    seed({ 'u:room:t1': [entry('x', { queuedAt: NaN })] });
    expect(stored()['u:room:t1'][0].queuedAt).toBeNull();
    expect(readQueue('u:room:t1')).toEqual([]);
  });

  it('discards an entry whose attachment lost its host path', () => {
    // The whole entry, not just the attachment: a message that goes out
    // without the file it was written about is worse than one to retype.
    seed({
      'u:room:t1': [
        entry('about the file', { attachments: [{ name: 'spec.pdf', size: 3 }] as never }),
        entry('plain text', { cid: 2 }),
      ],
    });
    expect(readQueue('u:room:t1').map((e) => e.text)).toEqual(['plain text']);
  });

  it('drops a malformed citation without losing the message', () => {
    seed({ 'u:room:t1': [entry('keep me', { replyTo: 'nonsense' as never, replyToMsgId: 0 })] });
    const [back] = readQueue('u:room:t1');
    expect(back.text).toBe('keep me');
    expect(back.replyTo).toBeUndefined();
    expect(back.replyToMsgId).toBeUndefined();
  });

  it('discards an entry with no text and nothing attached', () => {
    // Restoring it would put a bubble on screen whose Send posts nothing.
    seed({
      'u:room:t1': [entry('   \n '), entry('real text', { cid: 2 })],
    });
    expect(readQueue('u:room:t1').map((e) => e.text)).toEqual(['real text']);
  });

  it('keeps an attachment-only entry, which has no text by design', () => {
    // The endpoint accepts a send carrying only files and describes it in the
    // prompt, so an empty `text` is an ordinary queued message here.
    seed({ 'u:room:t1': [entry('', { attachments: [attachment()] })] });
    expect(readQueue('u:room:t1')).toHaveLength(1);
  });

  it('recovers the citation when only the quote survived', () => {
    // The two fields are one fact. Split, the POST goes out without the parent
    // the message was written against.
    seed({
      'u:room:t1': [entry('as I said', { replyTo: { msgId: 7, role: 'assistant' } })],
    });
    expect(readQueue('u:room:t1')[0].replyToMsgId).toBe(7);
  });

  it('keeps the citation the POST carries when only the id survived', () => {
    // The other direction: no optimistic quote to render, but the reply still
    // goes out as a reply.
    seed({ 'u:room:t1': [entry('as I said', { replyTo: 'nonsense' as never, replyToMsgId: 7 })] });
    const [back] = readQueue('u:room:t1');
    expect(back.replyTo).toBeUndefined();
    expect(back.replyToMsgId).toBe(7);
  });

  it('clamps a timestamp written in the future rather than letting it never expire', () => {
    // `now - queuedAt` stays negative for a future stamp, so it outlives the
    // TTL by construction and sorts its room ahead of every real message for
    // as long as it is stored. A clock that was wrong at write time reaches
    // this without anyone hand-editing anything.
    seed({ 'u:room:t1': [entry('from tomorrow', { queuedAt: Date.now() + 5 * DAY })] });
    expect(readQueue('u:room:t1')[0].queuedAt).toBe(Date.now());
    // The clamp is durable: any later write re-stores what the read produced,
    // so the entry ages from the moment it was first read back.
    writeQueue('u:room:t2', [entry('somewhere else')]);
    expect(stored()['u:room:t1'][0].queuedAt).toBe(Date.now());
    vi.setSystemTime(Date.now() + QUEUE_TTL_MS + 1);
    expect(readQueue('u:room:t1')).toEqual([]);
  });

  it('discards a timestamp outside the range a Date can hold', () => {
    // `new Date(1e20).toISOString()` throws RangeError, and the chat store
    // builds the restored row's timestamp that way — inside the transcript
    // rebuild, so one hand-edited number would cost the whole room's history
    // load rather than its own entry.
    seed({ 'u:room:t1': [entry('too far ahead', { queuedAt: 1e20 })] });
    expect(readQueue('u:room:t1')).toEqual([]);
  });

  it('reads a missing hold flag as unheld rather than as a hold', () => {
    seed({ 'u:room:t1': [{ cid: 1, text: 'x', attachments: [], queuedAt: Date.now() }] });
    expect(readQueue('u:room:t1')[0].held).toBe(false);
  });
});

describe('sendQueue — size bounds', () => {
  it('keeps the per-entry cap within the whole-map budget', () => {
    // The relationship the eviction rule rests on: the head of the room just
    // written is always kept, which only holds while one capped entry fits.
    expect(MAX_QUEUE_CHARS).toBeLessThanOrEqual(MAX_QUEUE_TOTAL_CHARS);
  });

  it('truncates an oversized message rather than dropping it', () => {
    writeQueue('u:room:t1', [entry('x'.repeat(MAX_QUEUE_CHARS + 5000))]);
    expect(readQueue('u:room:t1')[0].text).toBe('x'.repeat(MAX_QUEUE_CHARS));
  });

  it('keeps the head of an oversized message, not the tail', () => {
    writeQueue('u:room:t1', [entry('START' + 'x'.repeat(MAX_QUEUE_CHARS))]);
    expect(readQueue('u:room:t1')[0].text.startsWith('START')).toBe(true);
  });

  it('does not split a surrogate pair at the truncation boundary', () => {
    const text = 'a'.repeat(MAX_QUEUE_CHARS - 1) + '😀' + 'b';
    writeQueue('u:room:t1', [entry(text)]);
    const kept = readQueue('u:room:t1')[0].text;
    expect(kept).toBe('a'.repeat(MAX_QUEUE_CHARS - 1));
  });

  it('clamps an oversized entry already in storage on read', () => {
    seed({ 'u:room:t1': [entry('y'.repeat(MAX_QUEUE_CHARS * 3))] });
    expect(readQueue('u:room:t1')[0].text.length).toBe(MAX_QUEUE_CHARS);
    writeQueue('u:room:t2', [entry('small')]);
    expect(stored()['u:room:t1'][0].text.length).toBe(MAX_QUEUE_CHARS);
  });

  it('caps one room at MAX_QUEUED_PER_ROOM, keeping the head', () => {
    // FIFO: the head is what drains next, so a cap trimming the front would
    // reorder what the user wrote and send the wrong message first.
    const many = Array.from({ length: MAX_QUEUED_PER_ROOM + 3 }, (_, i) =>
      entry(`message ${i}`, { cid: i + 1 }),
    );
    writeQueue('u:room:t1', many);
    const back = readQueue('u:room:t1');
    expect(back).toHaveLength(MAX_QUEUED_PER_ROOM);
    expect(back[0].text).toBe('message 0');
    expect(back.at(-1)!.text).toBe(`message ${MAX_QUEUED_PER_ROOM - 1}`);
  });

  it('caps the number of rooms, evicting the oldest', () => {
    for (let i = 0; i < MAX_QUEUE_ROOMS + 5; i++) {
      vi.setSystemTime(Date.now() + 1000);
      writeQueue(`u:room:t${i}`, [entry(`message ${i}`)]);
    }
    expect(Object.keys(stored())).toHaveLength(MAX_QUEUE_ROOMS);
    expect(readQueue('u:room:t0')).toEqual([]);
    expect(readQueue('u:room:t4')).toEqual([]);
    expect(readQueue('u:room:t5').map((e) => e.text)).toEqual(['message 5']);
    expect(readQueue(`u:room:t${MAX_QUEUE_ROOMS + 4}`)).toHaveLength(1);
  });

  it('bounds the whole map, evicting the oldest rooms to fit', () => {
    const big = 'x'.repeat(MAX_QUEUE_CHARS);
    const roomCount = Math.ceil(MAX_QUEUE_TOTAL_CHARS / MAX_QUEUE_CHARS) + 2;
    for (let i = 0; i < roomCount; i++) {
      vi.setSystemTime(Date.now() + 1000);
      writeQueue(`u:room:t${i}`, [entry(big)]);
    }
    const total = Object.values(stored()).reduce(
      (n, entries) => n + entries.reduce((m, e) => m + e.text.length, 0),
      0,
    );
    expect(total).toBeLessThanOrEqual(MAX_QUEUE_TOTAL_CHARS);
    expect(readQueue(`u:room:t${roomCount - 1}`)).toHaveLength(1);
    expect(readQueue('u:room:t0')).toEqual([]);
  });

  it('trims a room entry by entry when its own queue outgrows the budget', () => {
    // One room at its per-room cap can exceed the whole-map budget on its own,
    // which is why the bound is applied entry by entry: the head survives
    // rather than the room being dropped whole.
    const big = 'x'.repeat(MAX_QUEUE_CHARS);
    const many = Array.from({ length: MAX_QUEUED_PER_ROOM }, (_, i) =>
      entry(big + i, { cid: i + 1 }),
    );
    const written = writeQueue('u:room:t1', many);
    expect(written.length).toBeGreaterThan(0);
    expect(written.length).toBeLessThan(MAX_QUEUED_PER_ROOM);
    expect(written[0].text.endsWith('0') || written[0].text.length === MAX_QUEUE_CHARS).toBe(true);
  });

  it('always stores the room just written, however large the rest', () => {
    const big = 'x'.repeat(MAX_QUEUE_CHARS);
    for (let i = 0; i < 6; i++) {
      vi.setSystemTime(Date.now() + 1000);
      writeQueue(`u:room:t${i}`, [entry(big)]);
    }
    vi.setSystemTime(Date.now() + 1000);
    writeQueue('u:room:new', [entry('a short one')]);
    expect(readQueue('u:room:new').map((e) => e.text)).toEqual(['a short one']);
  });

  it('keeps the room just written when every entry shares one timestamp', () => {
    // The tie the `keep` argument exists for: ordering by age alone resolves a
    // tie by insertion order, so the room a caller just wrote is the one
    // evicted — the single outcome no caller can work around.
    const big = 'x'.repeat(MAX_QUEUE_CHARS - 1);
    for (let i = 0; i < 6; i++) writeQueue(`u:room:t${i}`, [entry(big + i)]);
    expect(readQueue('u:room:t5')).toHaveLength(1);
  });

  it('a queue that fits does not make the next room unwritable', () => {
    // ISSUE-216's shape, on this key: the oversized write has to *fit* and
    // then poison every later write for the bug to exist at all. jsdom has no
    // localStorage on an opaque origin, so `vitest-setup.ts` installs a plain
    // object and the ceiling has to be stubbed on that object.
    const quota = 1_000_000;
    const real = localStorage.setItem.bind(localStorage);
    let refused = 0;
    const spy = vi.spyOn(localStorage, 'setItem').mockImplementation((key, value) => {
      if (value.length > quota) {
        refused++;
        const err = new Error('quota');
        err.name = 'QuotaExceededError';
        throw err;
      }
      real(key, value);
    });
    try {
      writeQueue('u:room:big', [entry('x'.repeat(quota - 50))]);
      expect(spy).toHaveBeenCalled();
      vi.setSystemTime(Date.now() + 1000);
      writeQueue('u:room:small', [entry('a short one')]);
      expect(refused).toBe(0);
      expect(readQueue('u:room:big')[0].text.length).toBe(MAX_QUEUE_CHARS);
      expect(readQueue('u:room:small').map((e) => e.text)).toEqual(['a short one']);
    } finally {
      spy.mockRestore();
    }
  });
});
