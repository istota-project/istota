// `ui/` is the unit the shared library takes whole, so its barrel has to
// name every component in it: a file the barrel skips is either dead or reached
// by a deep import, and both go missing on extraction. The few components only
// another primitive mounts (`NavLink` inside `HeaderNav`, `NoticeDrawer` inside
// `AppShell`) live in `internal/`, which the barrel never exports.
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const uiDir = join(process.cwd(), 'src/lib/components/ui');

/** Test scaffolding that sits beside the components it drives. */
const isScaffold = (name: string) =>
  /\.harness\.svelte$|Fixture\.svelte$|Harness\.test\.svelte$|\.test\.svelte$/.test(name);

function components(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) components(path, out);
    else if (entry.endsWith('.svelte') && !isScaffold(entry)) out.push(relative(uiDir, path));
  }
  return out;
}

const barrel = readFileSync(join(uiDir, 'index.ts'), 'utf8');
const exported = new Set(
  [...barrel.matchAll(/from '\.\/([^']+\.svelte)'/g)].map((match) => match[1]),
);

describe('the ui barrel', () => {
  const all = components(uiDir);

  it('finds the components it is meant to judge', () => {
    expect(all).toContain('Button.svelte');
    expect(all).toContain('internal/NavLink.svelte');
  });

  it('exports every component outside internal/', () => {
    const missing = all.filter((file) => !file.startsWith('internal/') && !exported.has(file));
    expect(missing).toEqual([]);
  });

  it('exports nothing from internal/', () => {
    expect([...exported].filter((file) => file.startsWith('internal/'))).toEqual([]);
  });

  it('names only files that exist', () => {
    expect([...exported].filter((file) => !all.includes(file))).toEqual([]);
  });
});
