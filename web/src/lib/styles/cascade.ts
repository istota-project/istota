import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { createRequire } from 'node:module';

/**
 * Reading the stylesheet the way the browser does, for the tests that assert
 * on it.
 *
 * `app.css` is an ordered list of `@import`s: `@minot/ui/styles.css` first,
 * which is itself the design system's ordered entry (tokens, the module
 * sheets, the table family, the primitives), then this app's own layers. A
 * test asking "does the cascade floor the input size?" has to ask the whole
 * cascade instead of one file.
 *
 * The import list is parsed out of the entry rather than restated here. The
 * ORDER of those imports is the entire contract of that file — ties that CSS
 * resolves by document order resolve the other way if it changes — so a copy
 * here would be a second source of truth for the one thing that is
 * load-bearing. Add a layer and these tests see it with no edit.
 *
 * A package import is resolved the way Node resolves it, through the
 * package's `exports`, so the sheets read are the ones installed. minot has a
 * reader of its own for its entry; this one reads the app's.
 *
 * Every reader takes the entry to start from, `app.css` by default. An import
 * whose target imports further sheets is expanded in place, in order, as the
 * browser does.
 */

const SRC = join(process.cwd(), 'src');

/** The app's stylesheet entry, the default for every reader below. */
export const APP_ENTRY = join(SRC, 'app.css');

// Node's resolver, anchored at web/, reading the package's `exports`. Not
// `import.meta.resolve`: vitest's module runner does not provide it.
const resolvePackage = createRequire(join(process.cwd(), 'package.json')).resolve;

const IMPORT = /@import\s+['"]([^'"]+)['"]\s*;/g;

function importsOf(path: string): string[] {
  const source = stripComments(readFileSync(path, 'utf8'));
  const imports = [...source.matchAll(IMPORT)].map((m) => m[1]);
  // An entry is an import list and nothing else; a sheet holding both would
  // lose its own rules when expanded, so it is refused rather than guessed at.
  if (imports.length > 0 && source.replace(IMPORT, '').trim() !== '') {
    throw new Error(`${path}: holds rules as well as imports`);
  }
  return imports;
}

/** The sheets `entry` loads, in the order it loads them, imports expanded. */
export function layerPaths(entry: string = APP_ENTRY): string[] {
  const out: string[] = [];
  for (const spec of importsOf(entry)) {
    const target = spec.startsWith('.') ? join(dirname(entry), spec) : resolvePackage(spec);
    if (importsOf(target).length > 0) out.push(...layerPaths(target));
    else out.push(target);
  }
  return out;
}

/**
 * The installed source of a minot component, for a test asserting on where
 * this app's own chrome meets one (the app nav against `ShellHeader`).
 */
export function minotComponentPath(name: string): string {
  return join(dirname(resolvePackage('@minot/ui/AGENTS.md')), 'dist/components', `${name}.svelte`);
}

/** One layer by basename, e.g. `tokens` or `primitives`. */
export function readLayer(name: string, entry: string = APP_ENTRY): string {
  const path = layerPaths(entry).find((p) => p.endsWith(`/${name}.css`));
  if (!path) throw new Error(`no such stylesheet layer: ${name}.css`);
  return readFileSync(path, 'utf8');
}

/**
 * Every layer concatenated in load order — what the browser ends up with.
 * Reach for this when asserting that a rule EXISTS somewhere, or that no
 * sheet overrides it. For a question about one layer specifically (is this
 * token declared on :root?), `readLayer` says so more precisely.
 */
export function readCascade(entry: string = APP_ENTRY): string {
  return layerPaths(entry)
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
