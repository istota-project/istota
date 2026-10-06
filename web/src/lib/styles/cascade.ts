import { readFileSync, readdirSync } from 'node:fs';
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

/** Body of the first block whose header starts at `needle`, braces balanced. */
export function blockAfter(source: string, needle: string): string | null {
  const at = source.indexOf(needle);
  if (at === -1) return null;
  const open = source.indexOf('{', at);
  if (open === -1) return null;
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, i);
    }
  }
  return null;
}

interface Rule {
  selector: string;
  body: string;
}

export function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** Flat `selector { body }` rules of a block body. Comments are stripped first:
 *  five in this tree quote a brace, which desynchronizes the walk. */
export function rules(body: string): Rule[] {
  const out: Rule[] = [];
  for (const m of stripComments(body).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selector: m[1].trim().replace(/\s+/g, ' '), body: m[2] });
  }
  return out;
}

export function styleFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) styleFiles(path, out);
    else if (/\.(svelte|css)$/.test(entry.name)) out.push(path);
  }
  return out;
}

/** Every `<style>` body in a component, or the whole file for a stylesheet. */
export function styleBlocks(file: string, source: string): string[] {
  if (file.endsWith('.css')) return [source];
  return [...source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]);
}
