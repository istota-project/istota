import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The money record-table shell — the toolbar/list/header/row rules every money
 * list page inherits, defined once in `lib/styles/moneyTable.css`.
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

const shell = readFileSync(join(process.cwd(), 'src/lib/styles/moneyTable.css'), 'utf8');

/** The declarations inside a top-level rule. */
const ruleBody = (selector: string): string => {
  const start = shell.indexOf(`\n${selector} {`);
  if (start === -1) throw new Error(`no ${selector} rule in lib/styles/moneyTable.css`);
  return shell.slice(start, shell.indexOf('}', start));
};

describe('.money-result-count', () => {
  // The count's own line box is shorter than the tier, so left to itself its
  // position is decided by whatever else is in the bar: on a toolbar that fits
  // on one line the min-height's leftover is split above and below it, and on
  // one that wraps there is no leftover and it sits flush on the padding — a
  // ~6px jump between two pages that look identical otherwise.
  it('reserves the field-tier height so its position does not depend on its siblings', () => {
    expect(ruleBody('.money-result-count')).toMatch(/min-height:\s*var\(--control-height-lg\)/);
  });

  it('centres its own text in that reserved box', () => {
    // min-height alone would only make the box taller and leave the text at the
    // top of it, which is the same defect pointing the other way.
    const body = ruleBody('.money-result-count');
    expect(body).toMatch(/display:\s*inline-flex/);
    expect(body).toMatch(/align-items:\s*center/);
  });
});

describe('.money-toolbar', () => {
  it('reserves a control row whether or not it holds controls', () => {
    // What makes a count-only bar the same height as one with filters, so the
    // table under it starts at the same place on every tab.
    expect(ruleBody('.money-toolbar')).toMatch(
      /min-height:\s*calc\(var\(--control-height-lg\)\s*\+\s*var\(--space-4\)\)/,
    );
  });
});

describe('the kebab in a row', () => {
  // Every other cell is text and wants the row's baseline. A kebab has no text,
  // so its baseline is the bottom of the icon box — which `baseline` lines up
  // with the text baseline and leaves it hanging above the row's centre.
  it('centres itself rather than taking the row baseline', () => {
    expect(ruleBody('.money-table-row .ui-kebab-trigger')).toMatch(/align-self:\s*center/);
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
   * alias. Shell classes themselves stay out — the layout owns those.
   */
  const rowAliases = (source: string): string[] =>
    [...source.matchAll(/class="([^"]*money-table-(?:row|header)[^"]*)"/g)]
      .flatMap((m) => m[1].split(/\s+/))
      .filter((name) => name && !name.startsWith('money-'));

  it.each(pages.map((p) => [p.slice(root.length + 1), p] as const))(
    '%s leaves the row block padding to the money layout',
    (_name, path) => {
      const source = readFileSync(path, 'utf8');
      // Comments out first, or a rule that merely *explains* the shared shell
      // is read as a selector naming it (the crude rule matcher below treats
      // everything up to a `{` as the selector).
      const style = source.slice(source.indexOf('<style>')).replace(/\/\*[\s\S]*?\*\//g, '');
      if (!style) return;

      const targets = ['money-table-row', 'money-table-header', ...rowAliases(source)];
      const offenders = [...style.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
        .filter(([, selector]) => targets.some((cls) => selector.includes(`.${cls}`)))
        .filter(([, , body]) => VERTICAL_PADDING.test(body))
        .map(([, selector]) => selector.trim());

      expect(offenders).toEqual([]);
    },
  );
});
