/**
 * What counts as an `@name` mention in web chat, in one place.
 *
 * Two readers have to agree: the markdown renderer, which styles a mention in
 * a message body, and the composer's `@` autocomplete, which inserts one. If
 * they drifted, an accepted suggestion could render as plain text.
 *
 * The boundary follows the server's (`addressed_to_bot_in_text` in
 * `transport/web/__init__.py`): an `@` not preceded by a word character or a
 * second `@`, and a name not followed by a word character or a hyphen,
 * case-insensitive. Python's `\w` is Unicode, so the classes here are written
 * as Unicode letters, digits and marks rather than JS's ASCII `\w`. So `@istota`
 * styled here is a message the speech gate reads as addressing the bot, and
 * `bob@alice.example` is not a mention. The one gap: the server also accepts
 * the bot's Talk account name, which the client is never told, so a mention by
 * that name addresses the bot and renders plain.
 *
 * Only names the caller supplies are matched, never any `@word`. The caller
 * passes room members' user ids and the bot's name: user ids are assigned by
 * the operator, so a guest cannot choose text that styles as a member. Display
 * names are deliberately not matched, for the same reason.
 */

export interface MentionTarget {
  /** Matched after the `@`, case-insensitively. */
  name: string;
  /** The viewer's own mention, which renders more prominently. */
  self?: boolean;
}

export interface MentionSpan {
  start: number;
  end: number;
  target: MentionTarget;
}

const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** A word character as Python's `re` reads one, approximately. */
const WORD = '\\p{L}\\p{N}\\p{M}_';

/** A matcher for `targets`, or null when there is nothing to match. Longest
 *  name first, so `@al` never claims the front of `@alice`. */
export function mentionMatcher(targets: readonly MentionTarget[]): RegExp | null {
  const names = targets
    .map((t) => t.name.trim())
    .filter((n) => n.length > 0)
    .sort((a, b) => b.length - a.length)
    .map(escapeRegExp);
  if (names.length === 0) return null;
  return new RegExp(`(?<![${WORD}@])@(${names.join('|')})(?![${WORD}-])`, 'giu');
}

/**
 * The `[start, end)` ranges of backtick code in plain text: an inline span or a
 * fenced block, a run of backticks closed by the next run of exactly the same
 * length. For text that is not going through markdown (a user's own row, the
 * composer), where the markdown parser is not there to say what is code. An
 * unclosed run is literal, as in CommonMark.
 *
 * A linear scan rather than a backreference regex: `(`+)…\1` backtracks through
 * every shorter opener on a long run, which is quadratic on text a room member
 * chooses and is re-run on every render of their message.
 */
export function codeRanges(text: string): [number, number][] {
  const runs: { start: number; len: number }[] = [];
  for (let i = 0; i < text.length;) {
    if (text[i] !== '`') {
      i++;
      continue;
    }
    let j = i;
    while (j < text.length && text[j] === '`') j++;
    runs.push({ start: i, len: j - i });
    i = j;
  }
  // For each run length, the indices (into `runs`) that have it, in order, and
  // how far along that list the scan has got. Indices only ever increase, so
  // each list is walked once.
  const byLen = new Map<number, number[]>();
  runs.forEach((r, idx) => {
    const list = byLen.get(r.len);
    if (list) list.push(idx);
    else byLen.set(r.len, [idx]);
  });
  const cursor = new Map<number, number>();
  const ranges: [number, number][] = [];
  for (let idx = 0; idx < runs.length; idx++) {
    const { start, len } = runs[idx];
    const list = byLen.get(len)!;
    let at = cursor.get(len) ?? 0;
    while (at < list.length && list[at] <= idx) at++;
    cursor.set(len, at);
    if (at === list.length) continue;
    const close = list[at];
    ranges.push([start, runs[close].start + len]);
    idx = close;
  }
  return ranges;
}

/** Whether `index` falls inside one of `ranges`. */
export function inCode(ranges: readonly [number, number][], index: number): boolean {
  return ranges.some(([start, end]) => index >= start && index < end);
}

/** Every mention of one of `targets` in `text`, in order. */
export function findMentions(
  text: string,
  targets: readonly MentionTarget[],
  matcher: RegExp | null = mentionMatcher(targets),
): MentionSpan[] {
  if (!matcher || !text.includes('@')) return [];
  const byName = new Map<string, MentionTarget>();
  for (const t of targets) {
    const key = t.name.trim().toLowerCase();
    // A name listed twice keeps its `self` if either entry has it.
    const prev = byName.get(key);
    if (!prev || t.self) byName.set(key, t);
  }
  const spans: MentionSpan[] = [];
  matcher.lastIndex = 0;
  for (const m of text.matchAll(matcher)) {
    const target = byName.get(m[1].toLowerCase());
    if (target) spans.push({ start: m.index, end: m.index + m[0].length, target });
  }
  return spans;
}

/** Every mention of one of `targets` in plain `text`, outside backtick code. */
export function findPlainMentions(text: string, targets: readonly MentionTarget[]): MentionSpan[] {
  const spans = findMentions(text, targets);
  if (spans.length === 0) return spans;
  const code = codeRanges(text);
  return spans.filter((s) => !inCode(code, s.start));
}

/** The text the composer inserts for a target, which `findMentions` matches. */
export function mentionText(name: string): string {
  return `@${name.trim()}`;
}
