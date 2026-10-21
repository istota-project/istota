import { afterAll, describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { APP_ENTRY, layerPaths, readCascade, readLayer, stripComments } from './cascade';

const dir = mkdtempSync(join(tmpdir(), 'cascade-'));
mkdirSync(join(dir, 'lib'));
writeFileSync(join(dir, 'lib/index.css'), "@import './tokens.css';\n@import './primitives.css';\n");
writeFileSync(join(dir, 'lib/tokens.css'), ':root { --a: 1; }\n');
writeFileSync(join(dir, 'lib/primitives.css'), '.b { color: red; }\n');
writeFileSync(join(dir, 'app-shell.css'), '.c { color: blue; }\n');
writeFileSync(join(dir, 'app.css'), "@import './lib/index.css';\n@import './app-shell.css';\n");
afterAll(() => rmSync(dir, { recursive: true, force: true }));

const names = (paths: string[]) => paths.map((path) => basename(path));

describe("the app's entry", () => {
  it('loads @minot/ui first, then the app layers', () => {
    const imports = [
      ...stripComments(readFileSync(APP_ENTRY, 'utf8')).matchAll(/@import\s+'([^']+)'/g),
    ].map((m) => m[1]);
    expect(imports[0]).toBe('@minot/ui/styles.css');
    expect(imports.slice(1)).toEqual([
      './lib/styles/app-tokens.css',
      './lib/styles/app-shell.css',
      './lib/styles/markdown.css',
    ]);
  });

  it('expands the package entry into the installed sheets, in its order', () => {
    const paths = layerPaths();
    expect(names(paths)).toEqual([
      'tokens.css',
      'settings.css',
      'cards.css',
      'table.css',
      'primitives.css',
      'app-tokens.css',
      'app-shell.css',
      'markdown.css',
    ]);
    for (const path of paths.slice(0, 5)) {
      expect(path).toMatch(/node_modules\/@minot\/ui\/dist\/styles\//);
    }
  });
});

describe('cascade', () => {
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

  it('resolves a package import through its exports', () => {
    const entry = join(dir, 'package.css');
    writeFileSync(entry, "@import '@minot/ui/tokens.css';\n");
    expect(layerPaths(entry)[0]).toMatch(/node_modules\/@minot\/ui\/dist\/styles\/tokens\.css$/);
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
