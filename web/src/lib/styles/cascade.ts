import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';

/**
 * Reading the stylesheet the way the browser does, for the tests that assert
 * on it.
 *
 * `app.css` used to be one 1267-line file and every style test simply read it.
 * It is now an ordered list of `@import`s across four layers (tokens, the
 * app-agnostic primitives, this app's chrome, rendered markdown) plus the
 * three module sheets, so a test asking "does app.css floor the input size?"
 * has to ask the whole cascade instead of one file.
 *
 * The import list is parsed out of `app.css` rather than restated here. That
 * matters more than it looks: the ORDER of those imports is the entire
 * contract of that file — ties that CSS resolves by document order resolve the
 * other way if it changes — so a copy here would be a second source of truth
 * for the one thing the split made load-bearing. Add a layer and these tests
 * see it with no edit.
 */

const SRC = join(process.cwd(), 'src');

/** The `@import` targets of app.css, in the order it declares them. */
export function layerPaths(): string[] {
  const entry = join(SRC, 'app.css');
  const css = readFileSync(entry, 'utf8');
  const out: string[] = [];
  for (const m of css.matchAll(/@import\s+['"]([^'"]+)['"]\s*;/g)) {
    out.push(join(dirname(entry), m[1]));
  }
  return out;
}

/** One layer by basename, e.g. `tokens` or `primitives`. */
export function readLayer(name: string): string {
  const path = layerPaths().find((p) => p.endsWith(`/${name}.css`));
  if (!path) throw new Error(`no such stylesheet layer: ${name}.css`);
  return readFileSync(path, 'utf8');
}

/**
 * Every layer concatenated in load order — what the browser ends up with.
 * Reach for this when asserting that a rule EXISTS somewhere, or that no
 * sheet overrides it. For a question about one layer specifically (is this
 * token declared on :root?), `readLayer` says so more precisely.
 */
export function readCascade(): string {
  return layerPaths()
    .map((p) => readFileSync(p, 'utf8'))
    .join('\n');
}
