import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';

/**
 * The app half of minot's `dateInputs` test. minot's `primitives.css` floors
 * the width of an empty temporal input, which iOS otherwise renders as a blank
 * sliver; minot pins the floor. What is left here: no stylesheet in this app
 * re-declares `min-width` on a temporal input, which is what defeated the
 * floor the first time (a page rule outranks a bare type selector, and one
 * restated `min-width: 0`).
 */

const SRC = resolve(process.cwd(), 'src');

const TEMPORAL_TYPES = ['date', 'datetime-local', 'time', 'month', 'week'];

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '');
}

function declaration(body: string, prop: string): string | null {
  const m = new RegExp(`(?:^|[;{\\s])${prop}\\s*:\\s*([^;}]+)`).exec(body);
  return m ? m[1].trim() : null;
}

/** Every `.svelte` / `.css` file under src, so a rule added anywhere is seen. */
function stylesheets(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) stylesheets(path, out);
    else if (/\.(svelte|css)$/.test(entry.name)) out.push(path);
  }
  return out;
}

describe('no stylesheet overrides the temporal input floor', () => {
  const offenders: string[] = [];
  for (const file of stylesheets(SRC)) {
    for (const m of stripComments(readFileSync(file, 'utf8')).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selector = m[1].replace(/\s+/g, ' ').trim();
      const targetsTemporal = TEMPORAL_TYPES.some((t) => selector.includes(`input[type='${t}']`));
      // A pseudo-element rule (the calendar picker icon) styles the shadow
      // child, not the input's own box, so it cannot reach the floor.
      if (!targetsTemporal || selector.includes('::')) continue;
      if (declaration(m[2], 'min-width') !== null) {
        offenders.push(`${file.slice(SRC.length + 1)}: ${selector}`);
      }
    }
  }

  it('leaves min-width to the library', () => {
    // Any page rule outranks the bare type selector, so restating min-width
    // here silently defeats the floor — which is how the health-history
    // filter shipped as two blank boxes.
    expect(offenders).toEqual([]);
  });
});
