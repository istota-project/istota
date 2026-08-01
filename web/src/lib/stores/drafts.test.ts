import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  readDraft,
  writeDraft,
  dropDraft,
  DRAFT_TTL_MS,
  MAX_DRAFTS,
  MAX_DRAFT_CHARS,
  MAX_DRAFTS_CHARS,
  DRAFT_STORAGE_KEY,
} from './drafts';

const DAY = 24 * 60 * 60 * 1000;

function stored(): Record<string, { text: string; at: number }> {
  return JSON.parse(localStorage.getItem(DRAFT_STORAGE_KEY) ?? '{}');
}

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-07-31T12:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('drafts', () => {
  it('round-trips a draft under its key', () => {
    writeDraft('room:3', 'half a thought');
    expect(readDraft('room:3')).toBe('half a thought');
  });

  it('keeps each key separate', () => {
    writeDraft('room:3', 'for A');
    writeDraft('room:9', 'for B');
    expect(readDraft('room:3')).toBe('for A');
    expect(readDraft('room:9')).toBe('for B');
  });

  it('returns empty for a key nothing was written under', () => {
    expect(readDraft('room:404')).toBe('');
  });

  it('preserves leading whitespace and newlines inside a draft', () => {
    writeDraft('room:3', '  line one\n\n  line two');
    expect(readDraft('room:3')).toBe('  line one\n\n  line two');
  });

  it('treats a blank draft as no draft', () => {
    writeDraft('room:3', 'something');
    writeDraft('room:3', '   \n ');
    expect(readDraft('room:3')).toBe('');
    expect(stored()['room:3']).toBeUndefined();
  });

  it('does not touch storage when clearing a key that has no draft', () => {
    writeDraft('room:3', 'kept');
    const before = localStorage.getItem(DRAFT_STORAGE_KEY);
    writeDraft('room:9', '');
    expect(localStorage.getItem(DRAFT_STORAGE_KEY)).toBe(before);
  });

  it('clearing removes only its own key', () => {
    writeDraft('room:3', 'for A');
    writeDraft('room:9', 'for B');
    writeDraft('room:3', '');
    expect(readDraft('room:3')).toBe('');
    expect(readDraft('room:9')).toBe('for B');
  });

  it('does not rewrite an unchanged draft', () => {
    // The composer flushes on unmount whether or not anything was typed, so
    // without this, merely visiting a room re-serializes the map and stamps a
    // fresh age — and a draft you never edit again would never age out.
    writeDraft('room:3', 'unchanged');
    const before = localStorage.getItem(DRAFT_STORAGE_KEY);
    vi.setSystemTime(Date.now() + DAY);
    writeDraft('room:3', 'unchanged');
    expect(localStorage.getItem(DRAFT_STORAGE_KEY)).toBe(before);
  });

  it('does not restore a draft older than the TTL', () => {
    writeDraft('room:3', 'stale');
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS + 1);
    expect(readDraft('room:3')).toBe('');
  });

  it('drops expired drafts on the next write', () => {
    writeDraft('room:3', 'stale');
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS + 1);
    writeDraft('room:9', 'fresh');
    expect(stored()['room:3']).toBeUndefined();
    expect(readDraft('room:9')).toBe('fresh');
  });

  it('keeps a draft that is old but still inside the TTL', () => {
    writeDraft('room:3', 'recent enough');
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS - DAY);
    writeDraft('room:9', 'fresh');
    expect(readDraft('room:3')).toBe('recent enough');
  });

  it('re-writing a draft refreshes its age', () => {
    writeDraft('room:3', 'first');
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS - DAY);
    writeDraft('room:3', 'second');
    vi.setSystemTime(Date.now() + 2 * DAY);
    expect(readDraft('room:3')).toBe('second');
  });

  it('caps the number of stored drafts, keeping the newest', () => {
    for (let i = 0; i < MAX_DRAFTS + 5; i++) {
      vi.setSystemTime(Date.now() + 1000);
      writeDraft(`room:${i}`, `draft ${i}`);
    }
    const keys = Object.keys(stored());
    expect(keys).toHaveLength(MAX_DRAFTS);
    // The first five written are the ones evicted.
    expect(readDraft('room:0')).toBe('');
    expect(readDraft('room:4')).toBe('');
    expect(readDraft('room:5')).toBe('draft 5');
    expect(readDraft(`room:${MAX_DRAFTS + 4}`)).toBe(`draft ${MAX_DRAFTS + 4}`);
  });

  it('survives a corrupt payload', () => {
    localStorage.setItem(DRAFT_STORAGE_KEY, 'not json');
    expect(readDraft('room:3')).toBe('');
    writeDraft('room:3', 'recovered');
    expect(readDraft('room:3')).toBe('recovered');
  });

  it('ignores entries that are not draft-shaped', () => {
    localStorage.setItem(
      DRAFT_STORAGE_KEY,
      JSON.stringify({
        'room:3': { text: 42, at: Date.now() },
        'room:4': { text: 'no timestamp' },
        'room:5': null,
        'room:6': { text: 'good', at: Date.now() },
      }),
    );
    expect(readDraft('room:3')).toBe('');
    expect(readDraft('room:4')).toBe('');
    expect(readDraft('room:5')).toBe('');
    expect(readDraft('room:6')).toBe('good');
  });

  it('ignores a payload that is an array rather than a map', () => {
    localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify(['nope']));
    expect(readDraft('room:3')).toBe('');
  });

  it('ignores an entry whose timestamp did not survive serialization', () => {
    // What a non-finite age actually looks like by the time it comes back:
    // JSON.stringify writes NaN and Infinity as null, and JSON.parse refuses
    // the literals outright. So null is the only shape that reaches here.
    localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify({ 'room:3': { text: 'x', at: NaN } }));
    expect(stored()['room:3'].at).toBeNull();
    expect(readDraft('room:3')).toBe('');
  });

  it('stores an expired draft afresh when its text is retyped verbatim', () => {
    // The read filtered on expiry and the write did not, so the retyped text
    // matched the stale entry, hit the already-stored no-op, and the draft
    // stayed invisible until some unrelated write happened to prune it.
    writeDraft('room:3', 'the same words');
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS + 1);
    writeDraft('room:3', 'the same words');
    expect(readDraft('room:3')).toBe('the same words');
  });

  it('dropDraft removes one entry and leaves the others', () => {
    writeDraft('room:3', 'for A');
    writeDraft('room:9', 'for B');
    dropDraft('room:3');
    expect(readDraft('room:3')).toBe('');
    expect(readDraft('room:9')).toBe('for B');
  });

  it('dropDraft does not touch storage for a key that has none', () => {
    writeDraft('room:9', 'for B');
    const before = localStorage.getItem(DRAFT_STORAGE_KEY);
    dropDraft('room:404');
    expect(localStorage.getItem(DRAFT_STORAGE_KEY)).toBe(before);
  });
});

describe('drafts — size bounds', () => {
  it('keeps the per-draft cap within the whole-map budget', () => {
    // The one relationship the eviction rule rests on: the newest entry is
    // always kept, which is only guaranteed while a single capped draft can
    // fit inside the budget on its own.
    expect(MAX_DRAFT_CHARS).toBeLessThanOrEqual(MAX_DRAFTS_CHARS);
  });

  it('truncates an oversized draft rather than dropping it', () => {
    writeDraft('room:3', 'x'.repeat(MAX_DRAFT_CHARS + 5000));
    expect(readDraft('room:3')).toBe('x'.repeat(MAX_DRAFT_CHARS));
  });

  it('keeps the head of an oversized draft, not the tail', () => {
    writeDraft('room:3', 'START' + 'x'.repeat(MAX_DRAFT_CHARS));
    expect(readDraft('room:3').startsWith('START')).toBe(true);
  });

  it('leaves a draft at exactly the cap alone', () => {
    const text = 'x'.repeat(MAX_DRAFT_CHARS);
    writeDraft('room:3', text);
    expect(readDraft('room:3')).toBe(text);
  });

  it('does not split a surrogate pair at the truncation boundary', () => {
    // Slicing mid-pair leaves a lone high surrogate, which round-trips through
    // JSON intact and then renders as a replacement character.
    const text = 'a'.repeat(MAX_DRAFT_CHARS - 1) + '😀' + 'b';
    writeDraft('room:3', text);
    const kept = readDraft('room:3');
    expect(kept).toBe('a'.repeat(MAX_DRAFT_CHARS - 1));
    expect(kept.length).toBe(MAX_DRAFT_CHARS - 1);
  });

  it('clamps an oversized entry already in storage on read', () => {
    // A draft written before the cap existed must not be able to re-poison the
    // map by being read back at full size and re-stored.
    localStorage.setItem(
      DRAFT_STORAGE_KEY,
      JSON.stringify({ 'room:3': { text: 'y'.repeat(MAX_DRAFT_CHARS * 3), at: Date.now() } }),
    );
    expect(readDraft('room:3').length).toBe(MAX_DRAFT_CHARS);
    writeDraft('room:9', 'small');
    expect(stored()['room:3'].text.length).toBe(MAX_DRAFT_CHARS);
  });

  it('bounds the whole map, evicting the oldest drafts to fit', () => {
    const big = 'x'.repeat(MAX_DRAFT_CHARS);
    const rooms = Math.ceil(MAX_DRAFTS_CHARS / MAX_DRAFT_CHARS) + 2;
    for (let i = 0; i < rooms; i++) {
      vi.setSystemTime(Date.now() + 1000);
      writeDraft(`room:${i}`, big + i);
    }
    const total = Object.values(stored()).reduce((n, d) => n + d.text.length, 0);
    expect(total).toBeLessThanOrEqual(MAX_DRAFTS_CHARS);
    // The newest survives; the oldest are the ones that went.
    expect(readDraft(`room:${rooms - 1}`).length).toBe(MAX_DRAFT_CHARS);
    expect(readDraft('room:0')).toBe('');
  });

  it('always stores the draft just written, however large the rest', () => {
    const big = 'x'.repeat(MAX_DRAFT_CHARS);
    for (let i = 0; i < 10; i++) {
      vi.setSystemTime(Date.now() + 1000);
      writeDraft(`room:${i}`, big + i);
    }
    vi.setSystemTime(Date.now() + 1000);
    writeDraft('room:new', 'a short one');
    expect(readDraft('room:new')).toBe('a short one');
  });

  it('a draft that fits does not make the next room unwritable', () => {
    // ISSUE-216 as reported: "no draft in any room is saved — including short
    // ones in rooms you never pasted into." Reaching that needs the oversized
    // draft to *fit* and then poison every later write, so the sizes here are
    // picked against the stubbed ceiling rather than being merely huge: a
    // draft that blows the quota on its own is refused, stores nothing, and
    // leaves the next room's write to succeed — which is not the bug.
    //
    // jsdom has no localStorage on an opaque origin, so `vitest-setup.ts`
    // installs a plain object. It is not a `Storage` instance, so the ceiling
    // has to be stubbed on that object; spying on `Storage.prototype` patches
    // something nothing calls, and the test then passes against any code.
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
      writeDraft('room:big', 'x'.repeat(quota - 50));
      expect(spy).toHaveBeenCalled();
      vi.setSystemTime(Date.now() + 1000);
      writeDraft('room:small', 'a short one');
      expect(refused).toBe(0);
      // Both survive: the oversized one keeps its head rather than being lost
      // whole, and the room that never saw the paste is unaffected.
      expect(readDraft('room:big').length).toBe(MAX_DRAFT_CHARS);
      expect(readDraft('room:small')).toBe('a short one');
    } finally {
      spy.mockRestore();
    }
  });

  it('keeps the draft just written when every entry shares one timestamp', () => {
    // The tie the `keep` argument exists for. Ordering by age alone resolves a
    // tie by insertion order, so without it the entry a caller just wrote is
    // the one evicted — the single outcome no caller can work around. Every
    // other eviction test advances the clock and so passes either way.
    const big = 'x'.repeat(MAX_DRAFT_CHARS - 1);
    for (let i = 0; i < 10; i++) writeDraft(`room:${i}`, big + i);
    expect(readDraft('room:9')).toBe(big + '9');
  });
});
