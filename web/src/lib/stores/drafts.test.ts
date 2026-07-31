import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readDraft, writeDraft, DRAFT_TTL_MS, MAX_DRAFTS, DRAFT_STORAGE_KEY } from './drafts';

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
});
