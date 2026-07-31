import { describe, expect, it } from 'vitest';
import { mergeRecords, trimBuffer, MAX_BUFFERED } from './logRecords';
import type { AdminLogRecord } from '$lib/api';

function rec(cursor: string, message = cursor): AdminLogRecord {
  return {
    cursor,
    timestamp: '2026-07-31T10:00:00',
    level: 'INFO',
    logger: 'istota.scheduler',
    message,
    task_id: null,
    user_id: null,
    source_type: null,
  };
}

describe('mergeRecords', () => {
  it('appends records the buffer has not seen', () => {
    const out = mergeRecords([rec('a'), rec('b')], [rec('c')]);
    expect(out.map((r) => r.cursor)).toEqual(['a', 'b', 'c']);
  });

  it('never produces a duplicate cursor', () => {
    // The transcript is a keyed {#each}; Svelte throws on a duplicate key in
    // production as well as dev, so a replayed record must not stack.
    const out = mergeRecords([rec('a'), rec('b')], [rec('b'), rec('c')]);
    expect(out.map((r) => r.cursor)).toEqual(['a', 'b', 'c']);
    expect(new Set(out.map((r) => r.cursor)).size).toBe(out.length);
  });

  it('survives a full replay of the buffer', () => {
    // Exactly what an EventSource reconnect delivers: the URL was built with
    // the seed cursor, so the server re-sends everything since.
    const buffer = [rec('a'), rec('b'), rec('c')];
    const out = mergeRecords(buffer, [rec('a'), rec('b'), rec('c')]);
    expect(out.map((r) => r.cursor)).toEqual(['a', 'b', 'c']);
  });

  it('replaces a redelivered record in place rather than dropping it', () => {
    // A traceback whose continuation lines arrived on a later poll grows.
    const out = mergeRecords(
      [rec('a'), rec('b', 'boom')],
      [rec('b', 'boom\n  File "x.py", line 3\nValueError')],
    );
    expect(out).toHaveLength(2);
    expect(out[1].message).toContain('ValueError');
  });

  it('keeps position when a record is replaced', () => {
    const out = mergeRecords([rec('a'), rec('b'), rec('c')], [rec('b', 'grown')]);
    expect(out.map((r) => r.cursor)).toEqual(['a', 'b', 'c']);
    expect(out[1].message).toBe('grown');
  });

  it('dedups within a single incoming batch', () => {
    const out = mergeRecords([], [rec('a'), rec('a', 'second')]);
    expect(out).toHaveLength(1);
    expect(out[0].message).toBe('second');
  });

  it('returns the buffer untouched for an empty batch', () => {
    const buffer = [rec('a')];
    expect(mergeRecords(buffer, [])).toBe(buffer);
  });
});

describe('trimBuffer', () => {
  it('leaves a short buffer alone and reports no trim', () => {
    const buffer = [rec('a'), rec('b')];
    const out = trimBuffer(buffer, 10);
    expect(out.records).toBe(buffer);
    expect(out.trimmed).toBe(false);
  });

  it('drops the oldest rows and reports the trim', () => {
    const buffer = [rec('a'), rec('b'), rec('c')];
    const out = trimBuffer(buffer, 2);
    expect(out.records.map((r) => r.cursor)).toEqual(['b', 'c']);
    // The caller must drop its "load older" cursor: it no longer abuts the top
    // of the buffer, so paging older would leave an invisible hole.
    expect(out.trimmed).toBe(true);
  });

  it('does not trim at exactly the ceiling', () => {
    const buffer = Array.from({ length: 5 }, (_, i) => rec(String(i)));
    expect(trimBuffer(buffer, 5).trimmed).toBe(false);
  });

  it('defaults to MAX_BUFFERED', () => {
    const buffer = Array.from({ length: MAX_BUFFERED + 5 }, (_, i) => rec(String(i)));
    expect(trimBuffer(buffer).records).toHaveLength(MAX_BUFFERED);
  });
});
