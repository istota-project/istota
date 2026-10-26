// The pure half of `vite.apps.config.ts`: what the apps build emits, the
// checks on the kit before it is inlined, and the manifest the daemon reads.
// Node-only; nothing in an app document imports it.

import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

/** The kit's scripts, copied verbatim, in the order a document inlines them. */
export const KIT_SCRIPTS = ['guard.js', 'istota-app.js', 'components.js'] as const;

/** The kit's stylesheet, built by Vite so its `@import`s of the tokens resolve. */
export const KIT_STYLESHEET = 'istota.css';

// The document builder inlines each file between <script> or <style> tags and
// refuses a script that would end its element early rather than escaping it,
// so the bytes hashed are the bytes approved. The kit is held to the same rule
// here, at build time, rather than discovered at the first install.
export function checkInlineable(name: string, text: string): void {
  if (name.endsWith('.css')) {
    if (/<\/style/i.test(text)) throw new Error(`apps kit ${name} contains "</style"`);
    return;
  }
  if (/<\/script/i.test(text)) throw new Error(`apps kit ${name} contains "</script"`);
  if (text.includes('<!--')) throw new Error(`apps kit ${name} contains "<!--"`);
}

/** Files a starter app folder ships: everything but its tests. */
function shipped(name: string): boolean {
  return !/\.test\.[cm]?[jt]s$/.test(name);
}

/**
 * Every file under `src/apps/starter/`, as paths relative to it with forward
 * slashes, sorted. Empty when there is no starter directory.
 */
export function starterFiles(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const found: string[] = [];
  const walk = (current: string): void => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const path = join(current, entry.name);
      if (entry.isSymbolicLink()) throw new Error(`starter app path ${path} is a symlink`);
      if (entry.isDirectory()) walk(path);
      else if (entry.isFile() && shipped(entry.name)) found.push(relative(dir, path));
    }
  };
  walk(dir);
  return found.map((path) => path.split(sep).join('/')).sort();
}

/**
 * `{relative path: sha256 hex}` for every file the build wrote under `outDir`,
 * except the manifest itself. Read by `istota.apps.kit`.
 */
export function manifestOf(outDir: string): Record<string, string> {
  const manifest: Record<string, string> = {};
  const walk = (current: string): void => {
    for (const name of readdirSync(current).sort()) {
      const path = join(current, name);
      if (statSync(path).isDirectory()) {
        walk(path);
        continue;
      }
      const key = relative(outDir, path).split(sep).join('/');
      if (key === 'manifest.json') continue;
      manifest[key] = createHash('sha256').update(readFileSync(path)).digest('hex');
    }
  };
  walk(outDir);
  return manifest;
}
