/**
 * The tree guard that keeps `@minot/ui/dateFormat` the only copy of the date
 * rules. The module and its own tests live in minot; this file holds the half
 * that reads istota's tree, which is why every "exactly one file" guard there
 * is "no file" here.
 *
 * An exact expected set rather than a ceiling. A `<=` comparison is what round
 * 1 of this spec measured going quietly blind — it stays green while the copies
 * it was meant to catch come back under a different name in a different file.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

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

  // Read once, keyed by path: several guards over ~700 files would otherwise
  // read each one several times.
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

  it('appends no midnight suffix of its own', () => {
    // A pattern rather than two `includes` calls: `"T00:00:00"` in double
    // quotes and a bare `T00:00` are the same copy coming back, and a literal
    // match would report the tree clean.
    expect(filesMatching(/T00:00(:00)?['"`]/)).toEqual([]);
  });

  it('never takes today from the UTC half of an ISO string', () => {
    // After local midnight west of Greenwich that is tomorrow. `localDateISO`
    // is the local calendar date.
    expect(filesMatching(/new Date\(\)\.toISOString\(\)\.(slice|substring)\(0,\s*10\)/)).toEqual(
      [],
    );
    expect(filesMatching(/function (localDate|localToday)\(/)).toEqual([]);
  });

  it('splits seconds into days and hours nowhere', () => {
    // Narrower than a bare `86400`, deliberately: the relative-time ladders the
    // module does not own each divide by it too, and a guard matching those
    // would have to carry an exemption list. Whitespace is loose so a reformat
    // or a named `DAY` constant does not slip past.
    expect(filesMatching(/%\s*86400\s*\)?\s*\/\s*3600/)).toEqual([]);
  });

  it('renders a relative time nowhere', () => {
    // The rendered sentinel rather than the arithmetic. Not anchored on a
    // closing quote: `'just now'` and a bare `just now` in Svelte markup are
    // the same copy coming back. The cost is that a comment using the phrase
    // reads as a violation, which is the direction to be wrong in.
    expect(filesMatching(/\bjust now\b/)).toEqual([]);
  });

  it('renders an interpolated `ago` suffix nowhere', () => {
    // The complement of the guard above, for a ladder written without a "just
    // now" rung. It anchors on the closing brace of an interpolation, so the
    // same words in prose do not match, and it does not require a trailing
    // quote, which anything after the word (`${n}d ago · stale`) would defeat.
    expect(filesMatching(/\}\s*\w*\s*\bago\b/)).toEqual([]);
  });

  it('formats a fixed two-decimal figure in exactly one file', () => {
    // The literal `2`, not the option name: `/admin`'s currency formatter and
    // the two portfolio pages take a variable digit count off their own data
    // and are a different rule.
    expect(filesMatching(/minimumFractionDigits:\s*2\b/)).toEqual(['lib/format.ts']);
  });

  /**
   * The guard the others could not be. Each of those greps a fragment of one
   * implementation, so a copy written differently is invisible to all of them;
   * naming the platform call catches any spelling. The exemption list is the
   * whole cost, explicit and short by design: a file goes on it with a reason
   * or it gets converted.
   */
  it('calls the platform date formatters only where this module says it may', () => {
    const EXEMPT: Record<string, string> = {
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
