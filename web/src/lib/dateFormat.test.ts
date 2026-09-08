/**
 * The guard that motivated this module, and the tree guard that keeps it the
 * only copy.
 *
 * The assertions below deliberately do not hardcode a rendered date: the output
 * is locale- and timezone-dependent, and a table of literals would say more
 * about the machine than about the code. What is asserted instead is the thing
 * that was actually broken — `Invalid Date` — plus the identities that pin the
 * option and fallback plumbing.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import type { RelativeFormatOptions } from '$lib/dateFormat';
import {
  formatDate,
  formatDateTime,
  formatDuration,
  formatMinutes,
  formatRelative,
} from '$lib/dateFormat';

describe('formatDate', () => {
  it('renders a bare YYYY-MM-DD as that calendar day', () => {
    // Local midnight, not UTC midnight — `new Date('2026-09-05')` is UTC and
    // renders as the 4th anywhere west of Greenwich.
    expect(formatDate('2026-09-05')).toBe(
      new Date(2026, 8, 5).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      }),
    );
  });

  it('renders a value that already carries a T instead of Invalid Date', () => {
    // The whole finding: three money components appended `T00:00:00`
    // unconditionally, so a full timestamp became `…ZT00:00:00`.
    const rendered = formatDate('2026-09-05T14:22:31Z');
    expect(rendered).not.toBe('Invalid Date');
    expect(rendered).toBe(
      new Date('2026-09-05T14:22:31Z').toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      }),
    );
  });

  it('renders a space-separated SQLite timestamp instead of Invalid Date', () => {
    const rendered = formatDate('2026-09-05 14:22:31');
    expect(rendered).not.toBe('Invalid Date');
    expect(rendered).toBe(formatDate('2026-09-05T14:22:31'));
  });

  it('leaves an RFC 822 feed date for the platform to parse', () => {
    // It has a space and a time part, so an unanchored `replace(' ', 'T')`
    // makes `Tue,T05 …` of it and loses a date the platform parses fine. Feed
    // entries carry this shape whenever feedparser could not normalise one.
    expect(formatDate('Tue, 05 Sep 2026 14:22:31 GMT', { locale: 'en-US' })).toBe('Sep 5, 2026');
  });

  it('returns the empty string for a missing value, and the override when given', () => {
    expect(formatDate(null)).toBe('');
    expect(formatDate(undefined)).toBe('');
    expect(formatDate('')).toBe('');
    expect(formatDate(null, { empty: '—' })).toBe('—');
  });

  it('returns an unparseable value as it arrived, never Invalid Date', () => {
    expect(formatDate('not a date')).toBe('not a date');
  });

  it('takes the caller Intl options in place of the default, not merged with it', () => {
    // `/health/bloodwork` asks for 2-digit month and day and no short month;
    // a merge would leave `month: 'short'` in and the option would do nothing.
    const opts: Intl.DateTimeFormatOptions = {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    };
    expect(formatDate('2026-09-05', opts)).toBe(
      new Date(2026, 8, 5).toLocaleDateString(undefined, opts),
    );
  });

  it('honours a pinned locale', () => {
    expect(formatDate('2026-09-05', { locale: 'en-US', month: 'short', day: 'numeric' })).toBe(
      'Sep 5',
    );
    expect(formatDate('2026-09-05', { locale: 'de-DE', month: 'short', day: 'numeric' })).not.toBe(
      'Sep 5',
    );
  });
});

describe('formatDateTime', () => {
  it('does not append a midnight suffix', () => {
    expect(formatDateTime('2026-09-05T14:22:31Z')).toBe(
      new Date('2026-09-05T14:22:31Z').toLocaleString(),
    );
  });

  it('carries its own empty and unparseable fallbacks', () => {
    expect(formatDateTime(null, { empty: 'never' })).toBe('never');
    expect(formatDateTime('')).toBe('');
    expect(formatDateTime('nonsense')).toBe('nonsense');
  });

  it('takes a space-separated SQLite timestamp, like formatDate does', () => {
    expect(formatDateTime('2026-09-05 14:22:31')).toBe(formatDateTime('2026-09-05T14:22:31'));
  });

  it('leaves an RFC 822 date alone, like formatDate does', () => {
    expect(formatDateTime('Tue, 05 Sep 2026 14:22:31 GMT')).not.toBe(
      'Tue, 05 Sep 2026 14:22:31 GMT',
    );
  });
});

describe('formatDuration', () => {
  it('renders two units at most', () => {
    expect(formatDuration(45)).toBe('45s');
    expect(formatDuration(90)).toBe('1m');
    expect(formatDuration(3600)).toBe('1h 00m');
    expect(formatDuration(3900)).toBe('1h 05m');
    expect(formatDuration(86400)).toBe('1d 0h');
    expect(formatDuration(200000)).toBe('2d 7h');
  });

  it('floors at zero rather than rendering a negative', () => {
    expect(formatDuration(-5)).toBe('0s');
    expect(formatDuration(Number.NaN)).toBe('0s');
  });
});

describe('formatMinutes', () => {
  it('drops the minutes on a whole hour', () => {
    expect(formatMinutes(45)).toBe('45m');
    expect(formatMinutes(120)).toBe('2h');
    expect(formatMinutes(150)).toBe('2h 30m');
  });

  it('renders the caller fallback for a missing value', () => {
    expect(formatMinutes(null)).toBe('');
    expect(formatMinutes(null, '—')).toBe('—');
  });
});

/**
 * The one ladder the four surfaces used to spell four ways.
 *
 * `now` is passed on every case rather than faking the clock: the assertions
 * are about which rung a given age lands on, and a frozen `Date.now` would put
 * the fixture's own arithmetic between the test and the thing under test.
 */
describe('formatRelative', () => {
  const NOW = new Date('2026-09-05T12:00:00Z');
  const at = (secondsAgo: number) => new Date(NOW.getTime() - secondsAgo * 1000).toISOString();
  const rel = (secondsAgo: number, opts: RelativeFormatOptions = {}) =>
    formatRelative(at(secondsAgo), { now: NOW, ...opts });

  it('walks one ladder: just now, minutes, hours, days', () => {
    expect(rel(0)).toBe('just now');
    expect(rel(59)).toBe('just now');
    expect(rel(60)).toBe('1m ago');
    expect(rel(3599)).toBe('59m ago');
    expect(rel(3600)).toBe('1h ago');
    expect(rel(86399)).toBe('23h ago');
    expect(rel(86400)).toBe('1d ago');
    expect(rel(29 * 86400)).toBe('29d ago');
  });

  it('floors rather than rounds', () => {
    // `DeviceTrackerCard` rounded, so 119 seconds read as "2 min ago" — a
    // figure larger than the time that had actually elapsed. The other three
    // floored.
    expect(rel(119)).toBe('1m ago');
    expect(rel(5400)).toBe('1h ago');
    expect(rel(1.5 * 86400)).toBe('1d ago');
  });

  it('falls back to an absolute date at thirty days', () => {
    // The unbounded day count `/admin`'s own CSS comment measured at "1234d
    // ago". Compared against this module's `formatDate` rather than a literal,
    // which would assert the machine's locale rather than the rule.
    expect(rel(30 * 86400)).toBe(formatDate(at(30 * 86400)));
    expect(rel(400 * 86400)).toBe(formatDate(at(400 * 86400)));
    expect(rel(400 * 86400)).not.toContain('ago');
    // The two lines above pin which branch was taken and nothing about what it
    // renders, since they compare against the exact call that branch makes.
    // A year is the one component of a date every locale spells the same way.
    expect(rel(400 * 86400)).toMatch(/\d{4}/);
    // And the rung below the boundary is still a count of days.
    expect(rel(30 * 86400 - 1)).toBe('29d ago');
  });

  it('reads a future timestamp as just now rather than counting backwards', () => {
    // `NotificationItem` and `/location` had no negative guard, so a row
    // stamped by a clock a minute ahead of the reader's rendered `-1m ago`.
    const future = new Date(NOW.getTime() + 90_000).toISOString();
    expect(formatRelative(future, { now: NOW })).toBe('just now');
    expect(formatRelative(future, { now: NOW, seconds: true })).toBe('just now');
  });

  it('renders seconds only where the caller asks for them', () => {
    expect(rel(42)).toBe('just now');
    expect(rel(42, { seconds: true })).toBe('42s ago');
    // `/admin` refreshes on a timer and this is its scheduler liveness
    // readout, so a figure written this instant reads as `0s ago` there — the
    // behaviour that option exists to keep.
    expect(rel(0, { seconds: true })).toBe('0s ago');
    // It reaches the first rung and no further.
    expect(rel(60, { seconds: true })).toBe('1m ago');
  });

  it('renders the caller fallback for a missing value', () => {
    expect(formatRelative(null)).toBe('');
    expect(formatRelative(undefined)).toBe('');
    expect(formatRelative('')).toBe('');
    expect(formatRelative(null, { empty: 'never' })).toBe('never');
    expect(formatRelative(null, { empty: '—' })).toBe('—');
  });

  it('returns an unparseable value as it arrived, never NaNd ago', () => {
    // `/location`'s copy had no guard at all. Every comparison against `NaN`
    // is false, so it fell through the whole ladder and rendered `NaNd ago`.
    expect(formatRelative('not a date', { now: NOW })).toBe('not a date');
    expect(formatRelative('not a date', { now: NOW })).not.toContain('NaN');
  });

  it('takes a space-separated timestamp, like the rest of the module', () => {
    // The separator rule, with the zone designator present so the instant is
    // unambiguous. This is the shape the columns feeding these surfaces
    // actually carry: `notifications.updated_at` goes through `db.iso_utc_now()`
    // and `/admin` normalises through `_iso_utc()`, both of which append `Z`.
    expect(formatRelative('2026-09-05 11:00:00Z', { now: NOW })).toBe('1h ago');
  });

  it('reads a zoneless timestamp as local time, which is what the platform does', () => {
    // Pinned because it is a trap rather than because it is reached: a bare
    // `datetime('now')` value carries no `Z`, and ES parses a zoneless
    // date-time as *local*, so such a value would be misread by the reader's
    // own UTC offset. Nothing on these four surfaces delivers that shape today
    // — the case above is why — and this fails loudly if one ever does.
    // An identity rather than a rung: which rung it lands on depends on the
    // runner's zone, but that it agrees with local 11:00 does not.
    expect(formatRelative('2026-09-05 11:00:00', { now: NOW })).toBe(
      formatRelative(new Date(2026, 8, 5, 11, 0, 0).toISOString(), { now: NOW }),
    );
  });
});

/**
 * The pin: one implementation, not twenty.
 *
 * An exact expected set rather than a ceiling. A `<=` comparison is what round
 * 1 of this spec measured going quietly blind — it stays green while the copies
 * it was meant to catch come back under a different name in a different file.
 */
describe('no second copy of the date coercion', () => {
  const SRC = resolve(__dirname, '..');

  function walk(dir: string): string[] {
    const out: string[] = [];
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) out.push(...walk(full));
      else if (/\.(svelte|ts)$/.test(name)) out.push(full);
    }
    return out;
  }

  // Read once, keyed by path: three guards over ~700 files is three reads of
  // each otherwise, and the first draft read every candidate twice within one
  // guard.
  const sources = new Map(
    walk(SRC)
      .filter((f) => !/\.test\.(ts|svelte)$/.test(f))
      .map((f) => [relative(SRC, f), readFileSync(f, 'utf8')] as const),
  );

  function filesMatching(pattern: RegExp): string[] {
    const hits: string[] = [];
    for (const [path, body] of sources) if (pattern.test(body)) hits.push(path);
    return hits.sort();
  }

  it('appends a midnight suffix in exactly one file', () => {
    // A pattern rather than two `includes` calls: `"T00:00:00"` in double
    // quotes and a bare `T00:00` are the same copy coming back, and a literal
    // match would report the tree clean.
    expect(filesMatching(/T00:00(:00)?['"`]/)).toEqual(['lib/dateFormat.ts']);
  });

  it('splits seconds into days and hours in exactly one file', () => {
    // Narrower than a bare `86400`, deliberately: the four relative-time
    // ladders this module does not own each divide by it too, and a guard
    // matching those would have to carry an exemption list — which is the
    // shape round 1 measured going blind. Whitespace is loose so a reformat
    // or a named `DAY` constant does not slip past.
    expect(filesMatching(/%\s*86400\s*\)?\s*\/\s*3600/)).toEqual(['lib/dateFormat.ts']);
  });

  it('renders a relative time in exactly one file', () => {
    // The rendered sentinel rather than the arithmetic. `86400` and `60000`
    // both have honest non-relative uses in this tree — a poll interval, a
    // trip duration, an age in years — so a guard on the division would carry
    // an exemption list longer than the thing it guards.
    //
    // Not anchored on a closing quote: `'just now'` and a bare `just now` in
    // Svelte markup are the same copy coming back, and requiring the quote
    // would report the tree clean for the second. The cost is that a comment
    // elsewhere using the phrase reads as a violation, which is the direction
    // to be wrong in — it is one grep to confirm and a reword to clear.
    expect(filesMatching(/\bjust now\b/)).toEqual(['lib/dateFormat.ts']);
  });

  it('renders an interpolated `ago` suffix in exactly one file', () => {
    // The complement of the guard above, for a ladder written without a "just
    // now" rung. It matches every spelling the four copies used — `}m ago`,
    // `} min ago`, `} h ago`, `}s ago`, `}d ago` — and does not match the same
    // words in a sentence of prose, since it anchors on the closing brace of
    // an interpolation.
    //
    // The trailing quote is deliberately *not* required. Requiring it was the
    // first spelling and it is defeated by anything after the word — a
    // `${n}d ago · stale`, a `${n}m ago (${src})` — which is exactly the
    // "quietly blind" shape the guard above this one was written against.
    // `\s*` rather than `\s?` for the same reason.
    expect(filesMatching(/\}\s*\w*\s*\bago\b/)).toEqual(['lib/dateFormat.ts']);
  });

  it('formats a fixed two-decimal figure in exactly one file', () => {
    // The literal `2`, not the option name: `/admin`'s currency formatter and
    // the two portfolio pages take a variable digit count off their own data
    // and are a different rule.
    expect(filesMatching(/minimumFractionDigits:\s*2\b/)).toEqual(['lib/format.ts']);
  });

  /**
   * The guard the other three could not be.
   *
   * Each of those greps a fragment of one *implementation* — the midnight
   * suffix, the day/hour split, the fixed decimals — so a copy written
   * differently is invisible to all three. That is not hypothetical: this
   * stage's first pass converted twenty-eight call sites and left
   * `briefings/settings`' `fmtLastRun` and four inline dates in `health/stats`
   * behind, and all three guards were green with them in the tree. Naming the
   * platform call instead catches any spelling.
   *
   * The exemption list is the whole cost, and it is explicit and short by
   * design. A file goes on it with a reason or it gets converted; a wildcard
   * or a `<=` comparison here would put the guard straight back where it was.
   */
  it('calls the platform date formatters only where this module says it may', () => {
    const EXEMPT: Record<string, string> = {
      'lib/dateFormat.ts': 'the implementation',
      'lib/format.ts': 'formatDecimal, its number-side sibling',
      'lib/usageFormat.ts': 'formatNumber, which is a number rule and not a date one',
      'routes/chat/+page.svelte':
        'dayLabel: Today / Yesterday / weekday / date, a relative rule of its own',
      'routes/money/portfolio/history/+page.svelte': 'a variable-digit money figure',
      'routes/money/portfolio/overview/+page.svelte': 'a variable-digit money figure',
      'routes/money/transactions/+page.svelte': 'a row count, grouped for the reader',
    };
    const hits = filesMatching(/\.toLocaleDateString\(|\.toLocaleString\(/);
    expect(hits.filter((f) => !(f in EXEMPT))).toEqual([]);
    // And the list does not outlive what it exempts.
    expect(Object.keys(EXEMPT).filter((f) => !hits.includes(f))).toEqual([]);
  });
});
