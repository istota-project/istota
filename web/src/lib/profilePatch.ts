// Which profile fields this page actually edited (ISSUE-096).
//
// `PUT /settings/profile` writes every key it is given, so sending the whole
// form back means an untouched field overwrites whatever set it since the page
// loaded. That is mostly harmless for fields only a human edits — but the
// scheduler now writes `timezone` on its own when the user travels, so an open
// tab saving an unrelated preference would silently revert the travel update
// and trigger another one on the next check.

import type { UserProfile } from './api';

/**
 * The subset of `edited` that differs from the snapshot the page loaded.
 *
 * `snapshotJson` is the `JSON.stringify` of the profile as fetched — the same
 * string the dirty check compares against, so the two cannot disagree about
 * what "changed" means. An unparseable or empty snapshot yields the full set:
 * without a baseline the safe answer is the caller's explicit intent, not
 * silently dropping their edits.
 */
export function changedProfileFields(
  edited: Partial<UserProfile>,
  snapshotJson: string,
): Partial<UserProfile> {
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
    // Structural compare: several of these fields are arrays and objects, and
    // the page rebinds them rather than mutating in place.
    if (JSON.stringify(value) !== JSON.stringify(snapshot[key])) {
      patch[key] = value;
    }
  }
  return patch as Partial<UserProfile>;
}
