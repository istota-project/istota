import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The admin tables that leave one column unsized.
 *
 * Each is `.grid`, which the settings shell defines as `table-layout: fixed;
 * width: 100%`. Under fixed layout a declared column width is honoured and the
 * column left unsized gets whatever is left over — so each table deliberately
 * leaves its widest-content column unsized, and the table's own `min-width` is
 * what buys it room. The wrapping `.table-scroll` then scrolls horizontally.
 *
 * That arrangement has a failure mode with no visible symptom in the CSS: add a
 * column, or drop the table's `min-width` at a breakpoint, and the unsized
 * column absorbs the whole shortfall. Nothing errors, nothing wraps oddly in
 * review, and the page looks right on the monitor it was written on. That is
 * ISSUE-276 — on the status page's Users table `Tokens 24h` and `Cost 24h` were
 * added, the desktop `min-width` was raised to match, and the mobile block kept
 * the `min-width: 0` it had from before those columns existed. Measured in the
 * browser at a 390px viewport, `col-24h` came out exactly 0px wide, with the
 * headings painted over each other. The Scheduled jobs table had the same
 * defect from the start (ISSUE-786): no `min-width` at all, and percentage
 * widths in its narrow block that left the Job column about 13px on a phone.
 *
 * So this does the arithmetic the browser does: at each breakpoint, sum the
 * widths the table declares and check it still has room left for the column it
 * left unsized.
 */

/**
 * One cascade scope: the top-level rules, or the body of one `@media
 * (max-width: Npx)` or `@container settings (max-width: Nrem)` block. `at` is
 * that threshold, in the block's own unit, `Infinity` for the rules outside
 * every block.
 */
type Scope = { at: number; css: string };

/** A page's style block, split into scopes, and the unit its thresholds are in. */
type Sheet = { scopes: Scope[]; unit: 'px' | 'rem' };

/**
 * The page's `<style>` block, comments blanked so prose cannot match as CSS.
 * Sliced past the tag itself: leaving `<style>` in makes it part of the first
 * rule's selector list, so whichever rule happens to be written first in the
 * block becomes unmatchable.
 */
const styleOf = (page: string): string => {
  const source = readFileSync(join(process.cwd(), page), 'utf8');
  const styleAt = source.indexOf('<style');
  return source
    .slice(source.indexOf('>', styleAt) + 1, source.lastIndexOf('</style>'))
    .replace(/\/\*[\s\S]*?\*\//g, '');
};

/** Split a style block into its base rules and its breakpoint blocks, in
 *  source order — which is also precedence order here, since the pages write
 *  their breakpoints widest-first. */
const readSheet = (css: string): Sheet => {
  const blocks: Scope[] = [];
  const units = new Set<'px' | 'rem'>();
  let base = '';
  let i = 0;

  while (i < css.length) {
    const start = css.slice(i).search(/@(media|container)\b/);
    if (start === -1) {
      base += css.slice(i);
      break;
    }
    const at = i + start;
    base += css.slice(i, at);

    const open = css.indexOf('{', at);
    let depth = 0;
    let end = open;
    for (; end < css.length; end++) {
      if (css[end] === '{') depth++;
      else if (css[end] === '}' && --depth === 0) break;
    }

    const prelude = css.slice(at, open).trim();
    // A viewport breakpoint is px and a container breakpoint is rem, which is
    // how the pages write them. Anything else — a `min-width`,
    // `prefers-reduced-motion` or `print` block, a container other than the
    // settings shell — would land here as a scope with no threshold. Left to
    // itself that reads as "applies at every width" and quietly corrupts the
    // cascade, so refuse it instead.
    const media = prelude.match(/^@media\s*\(max-width:\s*(\d+(?:\.\d+)?)px\)$/);
    const container = prelude.match(
      /^@container\s+settings\s*\(max-width:\s*(\d+(?:\.\d+)?)rem\)$/,
    );
    if (media) units.add('px');
    else if (container) units.add('rem');
    else throw new Error(`adminTables.test cannot place this block: ${prelude}`);

    blocks.push({ at: Number((media ?? container)![1]), css: css.slice(open + 1, end) });
    i = end + 1;
  }

  // A viewport threshold and a container threshold are on different axes: the
  // container is the viewport less whatever the shell puts around it, and this
  // file does not know how much that is. A page mixing the two cannot be put in
  // one order, so say so rather than guess.
  if (units.size > 1) {
    throw new Error('adminTables.test cannot order @media and @container blocks on one page');
  }
  return { scopes: [{ at: Infinity, css: base }, ...blocks], unit: [...units][0] ?? 'px' };
};

/** Every top-level rule in a scope, as (selector list, declarations). */
const rules = (scopeCss: string): [string[], string][] =>
  [...scopeCss.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, sel, body]) => [
    sel.split(',').map((s) => s.trim()),
    body,
  ]);

/**
 * The value of `prop` for `selector` once every scope active at width `width`
 * has been applied. Later scopes win, which is what the browser does for two
 * rules of equal specificity.
 */
const declaredFor = (
  sheet: Sheet,
  width: number,
  selector: string,
  prop: string,
): string | undefined => {
  let value: string | undefined;
  for (const scope of sheet.scopes) {
    if (scope.at < width) continue;
    for (const [selectors, body] of rules(scope.css)) {
      if (!selectors.includes(selector)) continue;
      const match = body.match(new RegExp(`(?:^|;)\\s*${prop}:\\s*([^;]+)`));
      if (match) value = match[1].trim();
    }
  }
  return value;
};

/**
 * A page spells a column two ways — `.users-grid .col-user` and, in places, a
 * bare `.col-user`. The scoped spelling is the more specific of the two and
 * wins wherever both exist, so ask for it first.
 */
const declared = (sheet: Sheet, width: number, grid: string, column: string, prop: string) =>
  declaredFor(sheet, width, `${grid} ${column}`, prop) ?? declaredFor(sheet, width, column, prop);

/**
 * A length in rem. Everything these tables declare has to be rem, so the check
 * refuses px or % rather than converting it: the reader picks the text scale —
 * `:root[data-font-size]` sets the root font-size to 110% or 120% in
 * lib/styles/tokens.css — and a table whose columns are rem and whose floor is
 * px loses its reserve as that number goes up. At 120% the users grid's columns
 * came to 883px inside the old 904px floor, leaving `col-24h` about 20px: the
 * same defect as ISSUE-276, reached by changing a setting rather than a
 * breakpoint. In rem on both sides the scale cancels and the reserve is the
 * same at every setting. A percentage is worse still: it is a share of a table
 * that is itself only as wide as the container, which is how the jobs table's
 * narrow block took the Job column to nothing (ISSUE-786).
 */
const rem = (value: string, what: string): number => {
  const match = value.match(/^([\d.]+)rem$/);
  expect(
    match,
    `${what} is "${value}" — it has to be rem, or it stops tracking the columns ` +
      `beside it as soon as the reader picks a larger text scale`,
  ).not.toBeNull();
  return Number(match![1]);
};

type Table = {
  /** The page holding the table, from `web/`. */
  page: string;
  grid: string;
  /** The column deliberately left unsized, which the leftover goes to. */
  unsized: string;
  /** Every column that carries a width. */
  sized: string[];
  /** What the unsized column needs, in rem. */
  floor: number;
};

const TABLES: Table[] = [
  {
    // The admin status page's Users table. `col-24h` carries the stacked bar
    // plus one chip, about nine rem of content, and the cell's own padding
    // (`var(--space-2)` a side, inside the width under the global border-box)
    // accounts for the tenth.
    page: 'src/routes/admin/+page.svelte',
    grid: '.users-grid',
    unsized: '.col-24h',
    sized: [
      '.col-user',
      '.col-total',
      '.col-failed',
      '.col-avg',
      '.col-tokens',
      '.col-cost',
      '.col-active',
    ],
    floor: 10,
  },
  {
    // The Scheduled jobs page's two tables (one snippet). The Job column holds
    // the job name and, under it, the last error's disclosure; twelve rem keeps
    // a hyphenated name like `daily-log-reminder` on one or two lines.
    page: 'src/routes/admin/jobs/+page.svelte',
    grid: '.jobs-grid',
    unsized: '.col-job',
    sized: [
      '.col-user',
      '.col-schedule',
      '.col-state',
      '.col-last',
      '.col-failures',
      '.col-actions',
    ],
    floor: 12,
  },
];

for (const table of TABLES) {
  const sheet = readSheet(styleOf(table.page));
  const { grid, unsized } = table;
  const thresholds = sheet.scopes.filter((s) => s.at !== Infinity).map((s) => s.at);
  /** Each declared breakpoint, plus a width above all of them. */
  const widths = [Infinity, ...thresholds];
  const label = (width: number) =>
    width === Infinity ? 'above every breakpoint' : `at ${width}${sheet.unit}`;

  describe(`the ${grid} table fits the width it declares`, () => {
    for (const width of widths) {
      it(`leaves ${unsized} room ${label(width)}`, () => {
        const floor = declaredFor(sheet, width, grid, 'min-width');
        // `.table-scroll` only scrolls what overflows it, so a table without a
        // min-width does not come out narrower — its unsized column comes out
        // shorter. This is the ISSUE-276 line itself: the mobile block used to
        // say `min-width: 0`.
        expect(floor, `${grid} declares no min-width ${label(width)}`).toBeDefined();
        const budget = rem(floor!, `${grid}'s min-width ${label(width)}`);

        let sized = 0;
        const breakdown: string[] = [];
        for (const column of table.sized) {
          if (declared(sheet, width, grid, column, 'display') === 'none') continue;
          const value = declared(sheet, width, grid, column, 'width');
          // Not `continue`: a column silently resolving to nothing is the one
          // failure that makes this assertion weaker instead of louder, since it
          // drops out of the sum and inflates the leftover.
          expect(value, `${column} declares no width ${label(width)}`).toBeDefined();
          const columnWidth = rem(value!, `${column}'s width ${label(width)}`);
          sized += columnWidth;
          breakdown.push(`${column} ${columnWidth}rem`);
        }

        expect(
          budget - sized,
          `${grid} ${label(width)} declares ${sized}rem of column widths ` +
            `(${breakdown.join(', ')}) inside a ${budget}rem table, leaving ` +
            `${budget - sized}rem for ${unsized} — fixed table layout takes the ` +
            `shortfall out of that column, so it collapses`,
        ).toBeGreaterThanOrEqual(table.floor);
      });
    }

    it('lists every column the markup has', () => {
      // The sum above is only as good as `sized`: a column added to the page
      // and not to this list drops out of the sum and inflates the leftover.
      const source = readFileSync(join(process.cwd(), table.page), 'utf8');
      const tableAt = source.search(new RegExp(`class="grid ${grid.slice(1)}"`));
      expect(tableAt, `${table.page} has no <table> for ${grid}`).toBeGreaterThan(-1);
      const head = source.slice(tableAt, source.indexOf('</thead', tableAt));
      const columns = [...head.matchAll(/<th\b[^>]*class="([^"]*)"/g)].flatMap(([, classes]) =>
        classes
          .split(/\s+/)
          .filter((c) => c.startsWith('col-'))
          .map((c) => `.${c}`),
      );
      expect(columns.sort()).toEqual([...table.sized, unsized].sort());
    });

    it(`leaves ${unsized} unsized at every breakpoint`, () => {
      // The arithmetic above only describes the table while exactly one column
      // is unsized. Giving it a width would not fail anything by itself, and
      // would quietly turn every check above into a statement about a table
      // that no longer exists.
      for (const width of widths) {
        const value = declared(sheet, width, grid, unsized, 'width');
        expect(value ?? 'auto', `${unsized} is sized ${label(width)}`).toBe('auto');
      }
    });
  });
}

describe('readSheet', () => {
  it('places a settings container query by its rem threshold', () => {
    const sheet = readSheet(
      '.t { min-width: 10rem; } @container settings (max-width: 40rem) { .t { min-width: 8rem; } }',
    );
    expect(sheet.unit).toBe('rem');
    expect(declaredFor(sheet, Infinity, '.t', 'min-width')).toBe('10rem');
    expect(declaredFor(sheet, 40, '.t', 'min-width')).toBe('8rem');
  });

  it('refuses a block it cannot place', () => {
    expect(() =>
      readSheet('@container other (max-width: 40rem) { .t { width: 1rem; } }'),
    ).toThrow();
    expect(() => readSheet('@media (min-width: 640px) { .t { width: 1rem; } }')).toThrow();
  });

  it('refuses a page mixing viewport and container breakpoints', () => {
    expect(() =>
      readSheet(
        '@media (max-width: 640px) { .t { width: 1rem; } } ' +
          '@container settings (max-width: 40rem) { .t { width: 2rem; } }',
      ),
    ).toThrow();
  });
});
