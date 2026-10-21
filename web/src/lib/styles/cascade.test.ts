import { afterAll, describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { APP_ENTRY, layerPaths, readCascade, readLayer } from './cascade';

// A consumer's entry that loads a library entry first, which is itself an
// ordered list of sheets: the shape the app's entry takes once the reusable
// layers ship as a package.
const dir = mkdtempSync(join(tmpdir(), 'cascade-'));
mkdirSync(join(dir, 'lib'));
writeFileSync(join(dir, 'lib/index.css'), "@import './tokens.css';\n@import './primitives.css';\n");
writeFileSync(join(dir, 'lib/tokens.css'), ':root { --a: 1; }\n');
writeFileSync(join(dir, 'lib/primitives.css'), '.b { color: red; }\n');
writeFileSync(join(dir, 'app-shell.css'), '.c { color: blue; }\n');
writeFileSync(join(dir, 'app.css'), "@import './lib/index.css';\n@import './app-shell.css';\n");
afterAll(() => rmSync(dir, { recursive: true, force: true }));

const names = (paths: string[]) => paths.map((path) => basename(path));

describe('cascade', () => {
  it("reads the app's own entry by default", () => {
    const order = names(layerPaths());
    expect(order[0]).toBe('tokens.css');
    expect(order).toEqual(names(layerPaths(APP_ENTRY)));
  });

  it('reads a library entry on its own', () => {
    expect(names(layerPaths(join(dir, 'lib/index.css')))).toEqual(['tokens.css', 'primitives.css']);
  });

  it('expands an entry it imports in place, keeping the order', () => {
    expect(names(layerPaths(join(dir, 'app.css')))).toEqual([
      'tokens.css',
      'primitives.css',
      'app-shell.css',
    ]);
  });

  it('reads one layer, or the whole cascade, from the entry it is given', () => {
    const entry = join(dir, 'app.css');
    expect(readLayer('primitives', entry)).toContain('.b');
    expect(readCascade(entry)).toMatch(/--a[\s\S]*\.b[\s\S]*\.c/);
  });

  it('says so rather than guessing at a package import', () => {
    const entry = join(dir, 'package.css');
    writeFileSync(entry, "@import 'some-package/styles.css';\n");
    expect(() => layerPaths(entry)).toThrow(/non-relative import/);
  });

  it('refuses an imported sheet that holds rules as well as imports', () => {
    writeFileSync(join(dir, 'mixed.css'), "@import './lib/tokens.css';\n.x { color: red; }\n");
    writeFileSync(join(dir, 'mixed-entry.css'), "@import './mixed.css';\n");
    expect(() => layerPaths(join(dir, 'mixed-entry.css'))).toThrow(/rules as well as imports/);
  });

  it('ignores an import that is commented out', () => {
    writeFileSync(join(dir, 'commented.css'), "/* @import './gone.css'; */\n.y { color: red; }\n");
    writeFileSync(join(dir, 'commented-entry.css'), "@import './commented.css';\n");
    expect(names(layerPaths(join(dir, 'commented-entry.css')))).toEqual(['commented.css']);
  });
});
