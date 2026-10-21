// `components/ui/` and `lib/ui/` are what the shared library takes: the
// primitives and the generic helpers they run on. Nothing in either may reach
// into the app (`$lib/api`, a domain store, a module's code), or the extraction
// carries the app with it. A product component belongs beside `ui/`, as the
// notification inbox and global search do.
//
// This is also the minot spec's stage 2 check, taken early: no file slated to
// move imports `$lib/` outside its set.
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const root = join(process.cwd(), 'src/lib');

/** What a file in the set may import from `$lib/`. */
const ALLOWED = [
  '$lib/components/ui',
  '$lib/ui/',
  // The notices store and these two helpers travel with the library; minot
  // moves them into the package when it is created.
  '$lib/stores/notices',
  '$lib/clipboard',
  '$lib/dateFormat',
];

/**
 * Named exceptions, each with what removes it. Empty: `Avatar` took a `src`
 * prop and `UserAvatar` beside `ui/` builds the URL. A new entry needs a
 * reason and a plan to take it off.
 */
const EXCEPTIONS: Record<string, string[]> = {};

function sources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) sources(path, out);
    else if (/\.(svelte|ts)$/.test(entry) && !/\.test\.(ts|svelte)$/.test(entry)) out.push(path);
  }
  return out;
}

const isScaffold = (path: string) => /\.harness\.svelte$|Fixture\.svelte$/.test(path);

function libImports(path: string): string[] {
  const text = readFileSync(path, 'utf8');
  const specifiers = [
    ...text.matchAll(/\bfrom\s+'([^']+)'/g),
    ...text.matchAll(/\bimport\s*\(\s*'([^']+)'\s*\)/g),
    ...text.matchAll(/^\s*import\s+'([^']+)'/gm),
  ].map((match) => match[1]);
  return specifiers.filter((s) => s.startsWith('$lib/'));
}

function outsideTheSet(path: string): string[] {
  const rel = relative(root, path);
  const excepted = EXCEPTIONS[rel] ?? [];
  return libImports(path)
    .filter((s) => !ALLOWED.some((prefix) => s === prefix || s.startsWith(prefix)))
    .filter((s) => !excepted.includes(s))
    .map((s) => `${rel} -> ${s}`);
}

describe('the extraction boundary', () => {
  const files = [
    ...sources(join(root, 'components/ui')).filter((path) => !isScaffold(path)),
    ...sources(join(root, 'ui')),
  ];

  it('finds the files it is meant to judge', () => {
    const names = files.map((path) => relative(root, path));
    expect(names).toContain('components/ui/Button.svelte');
    expect(names).toContain('ui/persisted.ts');
  });

  it('imports nothing from $lib outside components/ui, lib/ui and the shared helpers', () => {
    expect(files.flatMap(outsideTheSet)).toEqual([]);
  });

  it('keeps every exception live, so a fixed one is taken off the list', () => {
    for (const [rel, specifiers] of Object.entries(EXCEPTIONS)) {
      const imports = libImports(join(root, rel));
      for (const specifier of specifiers) expect(imports).toContain(specifier);
    }
  });

  it('reaches no relative path outside the set', () => {
    const escapes = files.flatMap((path) => {
      const text = readFileSync(path, 'utf8');
      return [...text.matchAll(/\bfrom\s+'(\.[^']*)'/g)]
        .map((match) => join(path, '..', match[1]))
        .filter((target) => {
          const rel = relative(root, target);
          return !rel.startsWith('components/ui') && !rel.startsWith('ui');
        })
        .map((target) => `${relative(root, path)} -> ${relative(root, target)}`);
    });
    expect(escapes).toEqual([]);
  });
});
