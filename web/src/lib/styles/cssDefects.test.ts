import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { rules, stripComments, styleBlocks, styleFiles } from './cascade';

/**
 * Guards for three CSS defects the 2026-10-18 audit found, each of which looked
 * fine in source and did nothing (or the wrong thing) in the browser:
 *
 * - `100vh` for a surface height, which web/AGENTS.md forbids: in the iOS app
 *   it leaves a band after the keyboard closes. Height comes from
 *   `--app-height` / `--kb-height`.
 * - `text-overflow: ellipsis` without `overflow` clipped and `white-space:
 *   nowrap`, which never truncates; four rules relied on it.
 * - a rule in a route file made of nothing but `:global(...)`, which leaks to
 *   every page once that route's chunk has loaded (money's `.error-badge`
 *   reached the error page's badge of the same name).
 */

const SRC = join(process.cwd(), 'src');

interface Sheet {
  file: string;
  css: string;
}

const sheets = (): Sheet[] =>
  styleFiles(SRC).flatMap((path) => {
    const file = relative(SRC, path);
    return styleBlocks(path, readFileSync(path, 'utf8')).map((css) => ({ file, css }));
  });

const all = sheets();

/** A selector with every `:global(...)` removed, parentheses balanced. */
function withoutGlobals(selector: string): string {
  let out = '';
  let i = 0;
  while (i < selector.length) {
    if (selector.startsWith(':global(', i)) {
      let depth = 0;
      let j = i + ':global'.length;
      for (; j < selector.length; j++) {
        if (selector[j] === '(') depth += 1;
        else if (selector[j] === ')') {
          depth -= 1;
          if (depth === 0) break;
        }
      }
      i = j + 1;
    } else {
      out += selector[i];
      i += 1;
    }
  }
  return out;
}

/** Split a selector list on top-level commas only. */
function selectorParts(selector: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < selector.length; i++) {
    if (selector[i] === '(') depth += 1;
    else if (selector[i] === ')') depth -= 1;
    else if (selector[i] === ',' && depth === 0) {
      parts.push(selector.slice(start, i).trim());
      start = i + 1;
    }
  }
  parts.push(selector.slice(start).trim());
  return parts;
}

describe('css defect guards', () => {
  it('finds the tree', () => {
    expect(all.length).toBeGreaterThan(100);
  });

  it('no stylesheet sizes anything with 100vh', () => {
    const offenders = all
      .filter(({ css }) => /\b100vh\b/.test(stripComments(css)))
      .map(({ file }) => file);
    expect(offenders, 'use var(--app-height, 100dvh) - var(--kb-height, 0px)').toEqual([]);
  });

  it('every ellipsis rule clips and stops wrapping', () => {
    const offenders: string[] = [];
    for (const { file, css } of all) {
      for (const { selector, body } of rules(css)) {
        if (!/text-overflow\s*:\s*ellipsis/.test(body)) continue;
        const clips = /(^|[\s;])overflow(-x)?\s*:\s*(hidden|clip)\b/.test(body);
        const noWrap = /white-space\s*:\s*nowrap/.test(body);
        // A multi-line clamp truncates through -webkit-line-clamp instead.
        const clamps = /line-clamp\s*:/.test(body);
        if (clamps || (clips && noWrap)) continue;
        offenders.push(`${file}: ${selector}`);
      }
    }
    expect(offenders, 'text-overflow needs overflow: hidden and white-space: nowrap').toEqual([]);
  });

  // Each is name-scoped by a container class, by the money- prefix that
  // stray-money-global reserves, or documented in web/AGENTS.md.
  const ALLOWED_ROUTE_GLOBALS = new Set([
    'routes/money/+layout.svelte: :global(.money-error-badge)',
    'routes/+error.svelte: :global(.app-content:has(> .error-page))',
    'routes/money/reports/+layout.svelte: :global(.report-frame .section-header)',
    'routes/money/reports/+layout.svelte: :global(.report-frame .section-header:first-child)',
    'routes/money/reports/+layout.svelte: :global(.report-frame .section-toggle)',
    'routes/money/reports/+layout.svelte: :global(.report-frame .section-toggle:hover)',
    'routes/money/reports/+layout.svelte: :global(.report-frame .section-toggle:hover .section-label)',
  ]);

  it('no route file writes a rule that is global all the way down', () => {
    const offenders: string[] = [];
    for (const { file, css } of all) {
      if (!file.startsWith('routes/') || !file.endsWith('.svelte')) continue;
      for (const { selector } of rules(css)) {
        if (selector.startsWith('@')) continue;
        for (const part of selectorParts(selector)) {
          if (!part.includes(':global(')) continue;
          if (withoutGlobals(part).replace(/[\s>+~]/g, '') !== '') continue;
          const key = `${file}: ${part}`;
          if (!ALLOWED_ROUTE_GLOBALS.has(key)) offenders.push(key);
        }
      }
    }
    expect(offenders, 'scope the rule under the page container, or prefix the class').toEqual([]);
  });

  it('the money layout keeps its error badge off the error page', () => {
    const css = all
      .filter(({ file }) => file === 'routes/money/+layout.svelte')
      .map(({ css }) => stripComments(css))
      .join('\n');
    expect(css).toContain(':global(.money-error-badge)');
    expect(css).not.toMatch(/:global\(\.error-badge\)/);
  });
});
