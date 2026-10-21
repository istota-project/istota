import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The money record-table shell — the toolbar rules every money list page
 * inherits, defined once in `lib/styles/moneyTable.css`, and the record rows
 * under it, which are table.css's `.record-*` family (money's own rows,
 * generalised and moved there so the taxes page and the file pane could use
 * them without importing a money sheet).
 *
 * Same reasoning as controlTier.test.ts: these are inherited-token and
 * reserved-height invariants that are invisible at any one call site, so a page
 * can silently opt out of them and nothing fails until someone eyeballs two
 * tabs side by side.
 *
 * The shell lived in the layout's own style block until it was 380 lines of
 * `:global()` wrappers; it is a plain stylesheet now, imported by that layout.
 * The selectors are bare as a result — no wrapper to match around them.
 */

describe('.toolbar--bar', () => {
  it('is what every money toolbar is drawn with', () => {
    const pages = [
      'transactions/+page.svelte',
      'business/work/+page.svelte',
      'business/invoices/+page.svelte',
      'portfolio/history/+page.svelte',
      'portfolio/overview/+page.svelte',
    ];
    for (const page of pages) {
      const source = readFileSync(join(process.cwd(), 'src/routes/money', page), 'utf8');
      expect(source, page).toMatch(/class="money-toolbar toolbar toolbar--bar/);
    }
  });
});

/**
 * One row rhythm per tier, owned by the shell.
 *
 * A page styles its own columns; the vertical rhythm is not a column. Two
 * portfolio tables used to tighten their block padding to a hairline inside a
 * `max-width: 640px` query and so sat at a different row height from work,
 * invoices and transactions on the same phone — a divergence invisible on
 * either page alone, which is exactly the class the file exists to catch.
 * Horizontal padding is deliberately left alone here: a narrow layout
 * legitimately re-indents a column.
 */
describe('the row rhythm belongs to the shell', () => {
  const moneyRoutes = (dir: string): string[] =>
    readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) return entry.name === 'node_modules' ? [] : moneyRoutes(path);
      return entry.name.endsWith('.svelte') ? [path] : [];
    });

  const root = join(process.cwd(), 'src/routes/money');
  const pages = moneyRoutes(root).filter((p) => p !== join(root, '+layout.svelte'));

  /** Vertical padding in any spelling, including the shorthand. */
  const VERTICAL_PADDING = /(^|[;{\s])padding(-block(-start|-end)?|-top|-bottom)?\s*:/;

  /**
   * Classes a page hangs on the shared row/header elements alongside the shell
   * class (`.holdings-row`), so a page can't dodge the rule by styling its own
   * alias. Shell classes themselves stay out — table.css owns those.
   */
  const rowAliases = (source: string): string[] =>
    [...source.matchAll(/class="([^"]*record-(?:row|header)[^"]*)"/g)]
      .flatMap((m) => m[1].split(/\s+/))
      .filter((name) => name && !name.startsWith('record-') && name !== 'micro-label');

  it.each(pages.map((p) => [p.slice(root.length + 1), p] as const))(
    '%s leaves the row block padding to the record shell',
    (_name, path) => {
      const source = readFileSync(path, 'utf8');
      // Comments out first, or a rule that merely *explains* the shared shell
      // is read as a selector naming it (the crude rule matcher below treats
      // everything up to a `{` as the selector).
      const style = source.slice(source.indexOf('<style>')).replace(/\/\*[\s\S]*?\*\//g, '');
      if (!style) return;

      const targets = ['record-row', 'record-header', ...rowAliases(source)];
      const offenders = [...style.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
        .filter(([, selector]) => targets.some((cls) => selector.includes(`.${cls}`)))
        .filter(([, , body]) => VERTICAL_PADDING.test(body))
        .map(([, selector]) => selector.trim());

      expect(offenders).toEqual([]);
    },
  );
});

/**
 * The record rows left money for table.css under new names, with no aliases
 * kept: two names for one row is how the six re-implementations started.
 */
describe('the old money row names are gone', () => {
  const RETIRED =
    /\bmoney-(?:table|table-header|table-row|sortable|sort-arrow|status|amount|kebab-spacer)\b/;

  const sources = (dir: string): string[] =>
    readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) return sources(path);
      return /\.(svelte|css)$/.test(entry.name) ? [path] : [];
    });

  it('no stylesheet or component uses one', () => {
    const offenders = sources(join(process.cwd(), 'src')).filter((path) =>
      RETIRED.test(readFileSync(path, 'utf8')),
    );
    expect(offenders).toEqual([]);
  });
});
