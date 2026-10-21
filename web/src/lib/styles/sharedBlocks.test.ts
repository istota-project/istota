import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The app half of minot's `sharedBlocks` test. `.form-error`, `.form-actions`,
 * `.kv` and `.card-head` are minot's shared blocks and their rules are pinned
 * there; this sweeps this app's own components for a page restating one.
 */

const svelteFiles = (): string[] => {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) walk(path);
      else if (entry.endsWith('.svelte')) out.push(path);
    }
  };
  walk(join(process.cwd(), 'src'));
  return out;
};

const styleBlock = (source: string): string => {
  const at = source.indexOf('<style');
  if (at === -1) return '';
  return source.slice(at).replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length));
};

/** The bodies of every bare `.<cls> { … }` rule in a stylesheet. */
const bareRuleBodies = (css: string, cls: string): string[] => {
  const out: string[] = [];
  const re = new RegExp(`^[ \\t]*\\.${cls}\\s*\\{([^}]*)\\}`, 'gm');
  for (const m of css.matchAll(re)) out.push(m[1]);
  return out;
};

describe('no page re-declares a shared block', () => {
  const files = svelteFiles();

  for (const cls of ['form-error', 'form-actions', 'kv', 'card-head'] as const) {
    it(`no .svelte file restates .${cls}`, () => {
      // A call site MAY declare the class to place or align it. What it may
      // not do is restate the block itself, because then two files answer the
      // same question and the page's answer wins silently.
      const restated = /(^|\s)(display|grid-template-columns|justify-content|color|font-size)\s*:/;
      const offenders: string[] = [];
      for (const f of files) {
        for (const body of bareRuleBodies(styleBlock(readFileSync(f, 'utf8')), cls)) {
          if (restated.test(body)) offenders.push(f.slice(f.indexOf('src/')));
        }
      }
      expect(offenders, `.${cls} belongs to minot's primitives.css`).toEqual([]);
    });
  }
});
