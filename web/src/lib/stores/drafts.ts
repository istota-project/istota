/**
 * Unsent composer text, kept per room in the browser.
 *
 * A draft is client-local for the same reason the theme is: it belongs to the
 * machine someone is typing on, not to their account, and it has to be there
 * the instant the composer mounts — before any request could answer. Round
 * tripping it through the server would also publish half-written text the user
 * has not decided to send, which is the one thing a draft is not.
 *
 * Everything lives under one storage key as a map, rather than a key per room.
 * That makes pruning a local decision rather than a scan of the whole origin's
 * storage, and the whole map is a few hundred bytes at the sizes involved.
 *
 * Keys are opaque strings; the chat page builds its own as
 * `<user>:room:<token>`. Both halves earn their place. The **token** rather
 * than the room id, because `web_chat_rooms.id` is `INTEGER PRIMARY KEY`
 * without `AUTOINCREMENT` and SQLite hands a freed rowid straight back out —
 * so a deleted room's draft would be inherited by the next room to take its
 * id. And the **user** in front of it, because a shared Talk room has one
 * token across every member, so on a browser profile two people take turns
 * using, a bare token would hand one person's half-written message to the
 * other.
 */
import { loadSetting, saveSetting } from './persisted';

export const DRAFT_STORAGE_KEY = 'chat.drafts';

/**
 * How long an untouched draft survives.
 *
 * Something has to bound the map: a room that is deleted, hidden or simply
 * never revisited leaves its draft behind forever otherwise, and nothing else
 * in the app would ever collect it. A month is long enough that the draft is
 * still there whenever anyone would plausibly come back for it, and short
 * enough that a forgotten one does not resurface a season later as a surprise.
 */
export const DRAFT_TTL_MS = 30 * 24 * 60 * 60 * 1000;

/** Second bound, on count rather than age, for a very busy month. */
export const MAX_DRAFTS = 50;

type Draft = { text: string; at: number };
type DraftMap = Record<string, Draft>;

/**
 * The stored map, with anything that is not draft-shaped dropped.
 *
 * Per-entry validation rather than a blanket `as DraftMap`: this text goes
 * straight into a textarea's value, and a hand-edited or half-written payload
 * should cost one draft rather than the field.
 */
function readAll(now = Date.now()): DraftMap {
  const raw = loadSetting<unknown>(DRAFT_STORAGE_KEY, null);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const out: DraftMap = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const draft = value as Partial<Draft> | null;
    // `Number.isFinite` rather than `typeof === 'number'` states the property
    // the rest of the module relies on, rather than one that happens to imply
    // it here. Nothing can currently deliver a non-finite age — JSON writes
    // NaN and Infinity as null and refuses to parse either literal — but both
    // would be corrosive if one ever arrived: every comparison against NaN is
    // false, so such an entry reads back yet can never be re-stored, and an
    // Infinity age outlives the TTL and sorts to the front of the cap forever.
    if (!draft || typeof draft.text !== 'string' || !Number.isFinite(draft.at)) continue;
    // Expiry is applied here so every caller shares one notion of what is
    // stored. `readDraft` filtered on the way out and this did not, so an
    // expired entry whose text the user retyped verbatim hit the
    // already-stored no-op in `writeDraft`: the stale `at` survived, the read
    // kept returning nothing, and the draft only came back to life when some
    // unrelated write pruned it away.
    if (now - (draft.at as number) >= DRAFT_TTL_MS) continue;
    out[key] = { text: draft.text, at: draft.at as number };
  }
  return out;
}

/** Expired entries out, newest `MAX_DRAFTS` kept. */
function prune(map: DraftMap, now: number): DraftMap {
  const live = Object.entries(map).filter(([, draft]) => now - draft.at < DRAFT_TTL_MS);
  live.sort((a, b) => b[1].at - a[1].at);
  return Object.fromEntries(live.slice(0, MAX_DRAFTS));
}

/**
 * The draft held under `key`, or empty if there is none.
 *
 * The TTL is applied on the way out as well as on write, so an expired draft
 * is never restored even when nothing has written since it aged out.
 */
export function readDraft(key: string): string {
  return readAll()[key]?.text ?? '';
}

/**
 * Remove one entry outright.
 *
 * Same effect as writing a blank, and named separately because the intent
 * differs: this is the room going away, not the field being emptied. The
 * blank-write path is how the composer clears a draft it still owns.
 */
export function dropDraft(key: string): void {
  const map = readAll();
  if (!(key in map)) return;
  delete map[key];
  saveSetting(DRAFT_STORAGE_KEY, prune(map, Date.now()));
}

/**
 * Hold `text` under `key`, or drop the entry when there is nothing to hold.
 *
 * Blank counts as nothing: a field holding only whitespace cannot be sent
 * (`canSend` trims), so restoring it would be restoring the appearance of a
 * draft. Passing blank is therefore also how a draft is dropped — the composer
 * clears one by flushing an emptied field, so there is no second code path
 * that could fall out of step with this one.
 *
 * Writing what is already stored is a no-op, and both no-op cases matter more
 * than they look. The composer flushes on unmount, so without them merely
 * *visiting* a room would rewrite the map — re-serializing up to `MAX_DRAFTS`
 * entries, and stamping a fresh `at` that pushes the TTL out. The age would
 * then measure time since you last looked at a draft rather than time since
 * you last touched it, and a draft you never edit again would never age out.
 */
export function writeDraft(key: string, text: string): void {
  const now = Date.now();
  const map = readAll(now);
  if (text.trim() === '') {
    if (!(key in map)) return;
    delete map[key];
  } else {
    if (map[key]?.text === text) return;
    map[key] = { text, at: now };
  }
  saveSetting(DRAFT_STORAGE_KEY, prune(map, now));
}
