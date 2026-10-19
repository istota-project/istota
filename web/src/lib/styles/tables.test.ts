import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { blockAfter, readCascade } from './cascade';

/**
 * The table family stays in table.css.
 *
 * Before it there were three copies of one `<table>` design — `.settings
 * .grid`, the health layout's `table.grid` and the bloodwork marker history —
 * plus a fourth private one in the file table, a second `.table-scroll`, and
 * eight copies of the divided list. They agreed by luck: the marker history
 * had drifted to a lighter header weight, the file table to sentence case, and
 * the settings shell's `.muted` had shadowed the global one inside every
 * settings table.
 *
 * So: the column header type is written once (`.table thead th`), no component
 * writes it out again on a `th` of its own, no component defines the scroll
 * wrapper, and no table goes back to the old `.grid` name, which also means CSS
 * grid everywhere else in the app and once matched a form's grid rule on the
 * page holding the table.
 */

const SRC = join(process.cwd(), 'src');

const sourceFiles = (...exts: string[]): string[] => {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) walk(path);
      else if (exts.some((e) => entry.endsWith(e)) && !entry.endsWith('.test.ts')) out.push(path);
    }
  };
  walk(SRC);
  return out;
};

const rel = (path: string): string => path.slice(SRC.length + 1);

const stripComments = (css: string): string =>
  css.replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length));

/** A component's `<style>` block, or a stylesheet whole, comments blanked. */
const styleOf = (path: string): string => {
  const source = readFileSync(path, 'utf8');
  if (!path.endsWith('.svelte')) return stripComments(source);
  const at = source.indexOf('<style');
  return at === -1 ? '' : stripComments(source.slice(at));
};

/** Every innermost rule, as its selector list and its declared properties. */
const rulesOf = (css: string): { selectors: string[]; props: string[] }[] =>
  [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({
    selectors: selector
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    props: body
      .split(';')
      .map((d) => d.slice(0, d.indexOf(':')).trim())
      .filter(Boolean),
  }));

/** The selector's subject (its last compound) is a `th`. */
const targetsTh = (selector: string): boolean => {
  const subject = selector
    .replace(/:global\(([^)]*)\)/g, '$1')
    .split(/[\s>+~]+/)
    .filter(Boolean)
    .pop();
  return !!subject && /^th(?![\w-])/.test(subject);
};

/**
 * What makes a header a column header: its size, its case and its tracking.
 * Weight and colour alone are left to a page — a row header's weight, the
 * bloodwork matrix's pinned date row.
 */
const HEADER_TYPE = new Set(['font-size', 'text-transform', 'letter-spacing']);

/** The table family's own sheet, and markdown.css, which styles tables someone else wrote. */
const OWNERS = new Set(['lib/styles/table.css', 'lib/styles/markdown.css']);

const scanned = sourceFiles('.svelte', '.css')
  .map((path) => ({ file: rel(path), css: styleOf(path) }))
  .filter(({ file }) => !OWNERS.has(file));

describe('table.css', () => {
  const cascade = readCascade();

  it('loads from app.css', () => {
    expect(cascade).toContain('.table thead th {');
  });

  it('gives the column header the .micro-label type', () => {
    const header = blockAfter(cascade, '.table thead th {');
    expect(header).toContain('font-size: var(--text-xs)');
    expect(header).toContain('font-weight: var(--weight-medium)');
    expect(header).toContain('text-transform: uppercase');
    expect(header).toContain('letter-spacing: var(--tracking-caps)');
    expect(header).toContain('color: var(--text-dim)');
  });

  it('hovers an interactive row in the card surface and fills a selected one raised', () => {
    // The table decision (Q9): hover is --surface-card, not --hover-bg, so the
    // hovered row and the selected row stay two different looks.
    expect(blockAfter(cascade, '.table--interactive tbody tr:hover {')).toContain(
      'background: var(--surface-card)',
    );
    expect(blockAfter(cascade, '.table tbody tr.is-selected {')).toContain(
      'background: var(--selected-bg)',
    );
  });

  it('has two densities and no more', () => {
    expect(blockAfter(cascade, '.table--dense {')).toContain('--table-cell-py: var(--space-1)');
    expect(cascade).not.toMatch(/\.table--roomy\b/);
  });
});

describe('the table family is not written again', () => {
  it('finds no th that restates the column header type', () => {
    const offenders: string[] = [];
    for (const { file, css } of scanned) {
      for (const rule of rulesOf(css)) {
        if (!rule.selectors.some(targetsTh)) continue;
        const restated = rule.props.filter((p) => HEADER_TYPE.has(p));
        if (restated.length)
          offenders.push(`${file}: ${rule.selectors.join(', ')} sets ${restated}`);
      }
    }
    expect(offenders, 'the header type is .table thead th, in table.css').toEqual([]);
  });

  it('finds no second .table-scroll', () => {
    // Placing the wrapper (a margin under a page's own container) is fine;
    // giving it the scroll again is the copy the settings shell and the health
    // layout each carried.
    const offenders = scanned
      .filter(({ css }) =>
        rulesOf(css).some(
          (r) =>
            r.selectors.some((s) => /\.table-scroll(?![\w-])/.test(s)) &&
            r.props.some((p) => p.startsWith('overflow')),
        ),
      )
      .map(({ file }) => file);
    expect(offenders, 'use the global .table-scroll from table.css').toEqual([]);
  });

  it('puts no table on the old .grid shell', () => {
    const offenders = sourceFiles('.svelte')
      .filter((path) =>
        /<table\b[^>]*\bclass="(?:[^"]*\s)?grid(?![\w-])/.test(readFileSync(path, 'utf8')),
      )
      .map(rel);
    expect(offenders, 'a table is .table now').toEqual([]);
  });
});

describe('targetsTh', () => {
  it('reads the subject of a selector', () => {
    expect(targetsTh('th')).toBe(true);
    expect(targetsTh('.history th')).toBe(true);
    expect(targetsTh('thead > th.num')).toBe(true);
    expect(targetsTh(':global(table.grid th)')).toBe(true);
    expect(targetsTh('th .sort-button')).toBe(false);
    expect(targetsTh('.thumb')).toBe(false);
    expect(targetsTh('thead')).toBe(false);
  });

  it('catches a planted header copy', () => {
    const planted = rulesOf('.x th { font-size: var(--text-xs); text-transform: uppercase; }');
    expect(planted[0].selectors.some(targetsTh)).toBe(true);
    expect(planted[0].props.filter((p) => HEADER_TYPE.has(p))).toEqual([
      'font-size',
      'text-transform',
    ]);
  });
});
