import type { AdminLogRecord } from '$lib/api';

/**
 * Buffer ceiling for a live tail. Without it an overnight tail on a busy
 * instance grows the DOM until the tab dies; the transcript is a window onto
 * the log, not a second copy of it.
 */
export const MAX_BUFFERED = 2000;

/**
 * Merge streamed records into an existing buffer, keyed on cursor.
 *
 * Idempotent by construction, because two paths legitimately redeliver a
 * record: an EventSource reconnect resumes from the cursor its URL was built
 * with, and the server re-reads a record whose continuation lines were still
 * arriving when it was first sent. The transcript renders a keyed `{#each}`,
 * and Svelte 5 throws on a duplicate key in production as well as dev — so an
 * append-only merge turns an ordinary network blip into a blank page.
 *
 * A redelivered record *replaces* rather than being dropped, so a traceback
 * whose tail arrived on a later poll grows in place.
 */
export function mergeRecords(
  existing: AdminLogRecord[],
  incoming: AdminLogRecord[],
): AdminLogRecord[] {
  if (incoming.length === 0) return existing;
  const index = new Map(existing.map((r, i) => [r.cursor, i]));
  const next = [...existing];
  for (const rec of incoming) {
    const at = index.get(rec.cursor);
    if (at === undefined) {
      index.set(rec.cursor, next.length);
      next.push(rec);
    } else {
      next[at] = rec;
    }
  }
  return next;
}

export interface TrimResult {
  records: AdminLogRecord[];
  /** True when rows were dropped off the top, so a "load older" cursor held by
   *  the caller no longer abuts the buffer and must be discarded. */
  trimmed: boolean;
}

export function trimBuffer(records: AdminLogRecord[], max = MAX_BUFFERED): TrimResult {
  if (records.length <= max) return { records, trimmed: false };
  return { records: records.slice(-max), trimmed: true };
}
