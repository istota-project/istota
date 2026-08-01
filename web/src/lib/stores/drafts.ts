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
 * storage. It also means every write serializes the whole map, so the map's
 * size is a shared resource: one entry large enough to exceed the origin's
 * quota makes the write fail for every room at once, and `saveSetting` swallows
 * that — so nothing is saved and nothing is reported. A paste is the ordinary
 * way past it. The two size bounds below exist so the draft store can no longer
 * be the cause of that: the per-draft bound is applied on read as well as on
 * write, so a map written before they existed cannot re-introduce it, and the
 * whole-map bound is applied on write, where the map is assembled. Neither
 * makes a refusal impossible — the quota belongs to the origin, not to this
 * key, and a refusal from anywhere else is still swallowed.
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

/**
 * How much of one draft is kept.
 *
 * Around ten thousand words — far past anything anyone types into a chat
 * composer, and reached only by pasting. An oversized draft therefore costs its
 * own tail rather than the map, which is the whole point: the alternative is a
 * write that fails for every room and says nothing.
 *
 * Deliberately well above `[web.chat] max_prompt_chars`, the server's own
 * ceiling on a send (32,000 by default). The draft is the durability copy of a
 * submitted message, so as long as the cap sits above what the server would
 * accept, truncation can only ever reach text that could not have been sent.
 * Raising that setting past this figure would change that.
 */
export const MAX_DRAFT_CHARS = 64 * 1024;

/**
 * How much the map holds in total, across every room.
 *
 * The per-draft cap alone does not bound the map: `MAX_DRAFTS` large drafts
 * would still add up past a browser's quota, which is counted in UTF-16 code
 * units and shared with everything else on the origin. This is deliberately a
 * small fraction of the ~5 MB browsers offer, since a draft store has no claim
 * on the rest of it.
 *
 * Counted in characters of draft text, not in the code units the serialized
 * payload costs — JSON escaping inflates that by a small constant factor,
 * around 2x for the newline-dense text a paste usually is. The headroom above
 * absorbs it; the figure is not a byte budget and should not be read as one.
 *
 * Must stay at or above `MAX_DRAFT_CHARS`, or a single capped draft could not
 * fit and the newest-always-kept guarantee below would not hold.
 */
export const MAX_DRAFTS_CHARS = 256 * 1024;

type Draft = { text: string; at: number };
type DraftMap = Record<string, Draft>;

/**
 * `text` cut to `MAX_DRAFT_CHARS`, without splitting a surrogate pair.
 *
 * A pair split down the middle leaves a lone high surrogate, which survives
 * JSON intact and then renders as a replacement character — so a truncated
 * draft would come back with a visible artifact on the end rather than simply
 * stopping. Backing off one unit costs the emoji and nothing else.
 */
function clampDraftText(text: string): string {
  if (text.length <= MAX_DRAFT_CHARS) return text;
  const cut = text.charCodeAt(MAX_DRAFT_CHARS - 1);
  const end = cut >= 0xd800 && cut <= 0xdbff ? MAX_DRAFT_CHARS - 1 : MAX_DRAFT_CHARS;
  return text.slice(0, end);
}

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
    // Clamped on the way in, not only on the way out, so an entry written
    // before the cap existed is capped the first time it is read and can never
    // be carried back into a write at full size. That cuts such an entry down
    // as a side effect of a write in some other room, which is data loss and
    // not only hygiene — accepted because an entry that size could not have
    // been stored under the bug this replaces, and because the alternative is
    // carrying it forward as the thing that breaks every write.
    out[key] = { text: clampDraftText(draft.text), at: draft.at as number };
  }
  return out;
}

/**
 * Expired entries out, then the newest kept until either bound is reached.
 *
 * `keep` is the entry the current write is about, and it goes to the front
 * regardless of age. Ordering by timestamp alone is not enough: two writes in
 * the same millisecond tie, and a tie resolves to insertion order, so the entry
 * a caller just wrote could be the one evicted — the one outcome no caller can
 * work around.
 */
function prune(map: DraftMap, now: number, keep?: string): DraftMap {
  const live = Object.entries(map).filter(([, draft]) => now - draft.at < DRAFT_TTL_MS);
  live.sort((a, b) => b[1].at - a[1].at);
  const first = keep === undefined ? -1 : live.findIndex(([key]) => key === keep);
  if (first > 0) live.unshift(...live.splice(first, 1));

  const out: DraftMap = {};
  let total = 0;
  for (const [key, draft] of live.slice(0, MAX_DRAFTS)) {
    // Stop rather than skip: entries are in eviction order already, so passing
    // over one that does not fit to reach a smaller, older one would keep the
    // wrong draft.
    if (total + draft.text.length > MAX_DRAFTS_CHARS) break;
    total += draft.text.length;
    out[key] = draft;
  }
  return out;
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
 *
 * Returns the text now held under `key`, which is `text` clamped, or empty
 * where the entry was dropped. Callers that keep their own copy of what they
 * wrote must keep *this* rather than what they passed: the composer records
 * the submitted text to recognise it again when the send acks, and comparing a
 * full-length copy against a clamped stored one makes every such test miss —
 * which would restore an already-sent message into the field as unsent text,
 * and leave its draft behind for good.
 */
export function writeDraft(key: string, text: string): string {
  const now = Date.now();
  const map = readAll(now);
  if (text.trim() === '') {
    if (key in map) {
      delete map[key];
      saveSetting(DRAFT_STORAGE_KEY, prune(map, now));
    }
    return '';
  }
  // Compared after clamping, and against an already-clamped stored value, so
  // typing on past the cap settles into the no-op instead of rewriting the map
  // on every debounce for text that cannot change what is stored.
  const clamped = clampDraftText(text);
  if (map[key]?.text === clamped) return clamped;
  map[key] = { text: clamped, at: now };
  saveSetting(DRAFT_STORAGE_KEY, prune(map, now, key));
  return clamped;
}
