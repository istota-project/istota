/**
 * The subset of `edited` that differs from a snapshot of the record as loaded.
 *
 * For a form that saves a patch rather than the whole record: an endpoint that
 * writes every key it is given would otherwise let an untouched field overwrite
 * whatever changed it since the page loaded.
 *
 * `snapshotJson` is the `JSON.stringify` of the record as fetched — the same
 * string a dirty check compares against, so the two cannot disagree about what
 * "changed" means. An unparseable or empty snapshot yields the full set:
 * without a baseline the safe answer is the caller's explicit intent, not
 * silently dropping their edits.
 */
export function changedFields<T extends object>(edited: T, snapshotJson: string): Partial<T> {
  let snapshot: Record<string, unknown> | null = null;
  try {
    const parsed = snapshotJson ? JSON.parse(snapshotJson) : null;
    if (parsed && typeof parsed === 'object') snapshot = parsed as Record<string, unknown>;
  } catch {
    snapshot = null;
  }
  if (snapshot === null) return { ...edited };

  const patch: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(edited)) {
    // Structural compare: fields may be arrays and objects, and a form
    // rebinds them rather than mutating in place.
    if (JSON.stringify(value) !== JSON.stringify(snapshot[key])) {
      patch[key] = value;
    }
  }
  return patch as Partial<T>;
}
