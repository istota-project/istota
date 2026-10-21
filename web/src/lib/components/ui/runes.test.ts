// svelte.config.js forces runes mode everywhere but `node_modules`, where the
// mode is inferred per file. Once these components ship in a package, one that
// happens to use no rune would compile in legacy mode, so each states the mode
// itself rather than depending on the consumer's config.
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const root = join(process.cwd(), 'src/lib/components/ui');

function svelteFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) svelteFiles(path, out);
    else if (entry.endsWith('.svelte')) out.push(path);
  }
  return out;
}

describe('runes mode', () => {
  const files = svelteFiles(root);

  it('finds the components, internal ones included', () => {
    const names = files.map((path) => relative(root, path));
    expect(names).toContain('Button.svelte');
    expect(names).toContain('internal/NoticeDrawer.svelte');
  });

  it('is declared by every component', () => {
    const missing = files
      .filter((path) => !readFileSync(path, 'utf8').includes('<svelte:options runes={true} />'))
      .map((path) => relative(root, path));
    expect(missing).toEqual([]);
  });
});
