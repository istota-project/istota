import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The admin status page's Users table.
 *
 * It is `.grid`, which the settings shell defines as `table-layout: fixed;
 * width: 100%`. Under fixed layout a declared column width is honoured and the
 * column left unsized gets whatever is left over — so `col-24h`, which carries
 * the int/auto summary, the stacked bar and one chip per source, is
 * deliberately the unsized one, and the table's own `min-width` is what buys it
 * room. The wrapping `.table-scroll` then scrolls horizontally.
 *
 * That arrangement has a failure mode with no visible symptom in the CSS: add a
 * column, or drop the table's `min-width` at a breakpoint, and the unsized
 * column absorbs the whole shortfall. Nothing errors, nothing wraps oddly in
 * review, and the page looks right on the monitor it was written on. That is
 * ISSUE-276 — `Tokens 24h` and `Cost 24h` were added, the desktop `min-width`
 * was raised to match, and the mobile block kept the `min-width: 0` it had from
 * before those columns existed. Measured in the browser at a 390px viewport,
 * `col-24h` came out exactly 0px wide, with the headings painted over each
 * other.
 *
 * So this does the arithmetic the browser does: at each breakpoint, sum the
 * widths the table declares and check it still has room left for the column it
 * left unsized.
 *
 * Only the users table. The scheduler table next to it leaves *four* columns
 * unsized at desktop and one at ≤640px, so "the remainder goes to the unsized
 * column" is not its model, and a check written as if it were passes on
 * arithmetic that describes nothing. It is also bounded by its container rather
 * than by a `min-width` it declares, which is a second model again. Neither is
 * this table's shape; guarding them properly is its own piece of work.
 */

const source = readFileSync(join(process.cwd(), 'src/routes/admin/+page.svelte'), 'utf8');

/**
 * The page's `<style>` block, comments blanked so prose cannot match as CSS.
 * Sliced past the tag itself: leaving `<style>` in makes it part of the first
 * rule's selector list, so whichever rule happens to be written first in the
 * block becomes unmatchable.
 */
const styleAt = source.indexOf('<style');
const css = source
  .slice(source.indexOf('>', styleAt) + 1, source.lastIndexOf('</style>'))
  .replace(/\/\*[\s\S]*?\*\//g, '');

/**
 * One cascade scope: the top-level rules, or the body of one `@media
 * (max-width: Npx)` block. `at` is that breakpoint, `Infinity` for the rules
 * outside every block.
 */
type Scope = { at: number; css: string };

/** Split the style block into its base rules and its media blocks, in source
 *  order — which is also precedence order here, since the page writes its
 *  breakpoints widest-first. */
const readScopes = (): Scope[] => {
  const media: Scope[] = [];
  let base = '';
  let i = 0;

  while (i < css.length) {
    const start = css.indexOf('@media', i);
    if (start === -1) {
      base += css.slice(i);
      break;
    }
    base += css.slice(i, start);

    const open = css.indexOf('{', start);
    let depth = 0;
    let end = open;
    for (; end < css.length; end++) {
      if (css[end] === '{') depth++;
      else if (css[end] === '}' && --depth === 0) break;
    }

    const prelude = css.slice(start, open);
    const at = prelude.match(/max-width:\s*(\d+)px/)?.[1];
    // A `min-width`, `prefers-reduced-motion` or `print` block is an ordinary
    // thing to add to this file, and every one of them would land here as a
    // scope with no breakpoint. Left to itself that reads as "applies at every
    // viewport" and quietly corrupts the cascade, so refuse it instead.
    if (at === undefined) {
      throw new Error(`adminTables.test cannot place this media query: ${prelude.trim()}`);
    }
    media.push({ at: Number(at), css: css.slice(open + 1, end) });
    i = end + 1;
  }

  return [{ at: Infinity, css: base }, ...media];
};

const scopes = readScopes();

/** Every top-level rule in a scope, as (selector list, declarations). */
const rules = (scopeCss: string): [string[], string][] =>
  [...scopeCss.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, sel, body]) => [
    sel.split(',').map((s) => s.trim()),
    body,
  ]);

/**
 * The value of `prop` for `selector` once every scope active at `viewport` has
 * been applied. Later scopes win, which is what the browser does for two rules
 * of equal specificity.
 */
const declaredFor = (viewport: number, selector: string, prop: string): string | undefined => {
  let value: string | undefined;
  for (const scope of scopes) {
    if (scope.at < viewport) continue;
    for (const [selectors, body] of rules(scope.css)) {
      if (!selectors.includes(selector)) continue;
      const match = body.match(new RegExp(`(?:^|;)\\s*${prop}:\\s*([^;]+)`));
      if (match) value = match[1].trim();
    }
  }
  return value;
};

/**
 * The page spells a column two ways — `.users-grid .col-user` and, in the
 * breakpoint blocks, a bare `.col-user`. The scoped spelling is the more
 * specific of the two and wins wherever both exist, so ask for it first.
 */
const declared = (viewport: number, grid: string, column: string, prop: string) =>
  declaredFor(viewport, `${grid} ${column}`, prop) ?? declaredFor(viewport, column, prop);

/**
 * A length in rem. Everything this table declares has to be rem, so the check
 * refuses px rather than converting it: the reader picks the text scale —
 * `:root[data-font-size]` sets the root font-size to 110% or 120% in
 * lib/styles/tokens.css — and a table whose columns are rem and whose floor is
 * px loses its reserve as that number goes up. At 120% the users grid's columns
 * came to 883px inside the old 904px floor, leaving `col-24h` about 20px: the
 * same defect as ISSUE-276, reached by changing a setting rather than a
 * breakpoint. In rem on both sides the scale cancels and the reserve is the
 * same at every setting.
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

const GRID = '.users-grid';

/** Every column that carries a width. `col-24h` is deliberately absent — it is
 *  the unsized one, and what is left over is what this file measures. */
const SIZED = [
  '.col-user',
  '.col-total',
  '.col-failed',
  '.col-avg',
  '.col-tokens',
  '.col-cost',
  '.col-active',
];

/**
 * What `col-24h` needs. The stacked bar plus one chip is about nine rem of
 * content, and the cell's own padding (`var(--space-2)` a side, inside the
 * width under the global border-box) accounts for the tenth.
 */
const FLOOR_REM = 10;

/** Each declared breakpoint, plus one viewport above all of them. */
const viewports = [1024, ...scopes.filter((s) => s.at !== Infinity).map((s) => s.at)];

describe('the admin Users table fits the width it declares', () => {
  for (const viewport of viewports) {
    it(`leaves col-24h room at ${viewport}px`, () => {
      const floor = declaredFor(viewport, GRID, 'min-width');
      // `.table-scroll` only scrolls what overflows it, so a table without a
      // min-width does not come out narrower — its unsized column comes out
      // shorter. This is the ISSUE-276 line itself: the mobile block used to
      // say `min-width: 0`.
      expect(floor, `${GRID} declares no min-width at ${viewport}px`).toBeDefined();
      const budget = rem(floor!, `${GRID}'s min-width at ${viewport}px`);

      let sized = 0;
      const breakdown: string[] = [];
      for (const column of SIZED) {
        if (declared(viewport, GRID, column, 'display') === 'none') continue;
        const width = declared(viewport, GRID, column, 'width');
        // Not `continue`: a column silently resolving to nothing is the one
        // failure that makes this assertion weaker instead of louder, since it
        // drops out of the sum and inflates the leftover.
        expect(width, `${column} declares no width at ${viewport}px`).toBeDefined();
        const value = rem(width!, `${column}'s width at ${viewport}px`);
        sized += value;
        breakdown.push(`${column} ${value}rem`);
      }

      expect(
        budget - sized,
        `${GRID} at ${viewport}px declares ${sized}rem of column widths ` +
          `(${breakdown.join(', ')}) inside a ${budget}rem table, leaving ` +
          `${budget - sized}rem for col-24h — fixed table layout takes the ` +
          `shortfall out of that column, so it collapses`,
      ).toBeGreaterThanOrEqual(FLOOR_REM);
    });
  }

  it('leaves col-24h unsized at every breakpoint', () => {
    // The arithmetic above only describes the table while exactly one column is
    // unsized. Giving col-24h a width would not fail anything by itself, and
    // would quietly turn every check above into a statement about a table that
    // no longer exists.
    for (const viewport of viewports) {
      const width = declared(viewport, GRID, '.col-24h', 'width');
      expect(width ?? 'auto', `col-24h is sized at ${viewport}px`).toBe('auto');
    }
  });
});
