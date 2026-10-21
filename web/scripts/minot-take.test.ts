import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { indentOf, subject } from './minot-take.mjs';

const script = resolve(process.cwd(), 'scripts/minot-take.mjs');

describe('minot:take', () => {
  it('refuses anything but an exact version, before running npm', () => {
    for (const arg of [[], ['^0.2.0'], ['latest'], ['0.2']]) {
      const run = spawnSync(process.execPath, [script, ...arg], { encoding: 'utf8' });
      expect(run.status, arg.join(' ')).toBe(2);
      expect(run.stderr).toMatch(/usage: npm run minot:take -- <version>/);
    }
  });

  it('keeps the indent the lockfile was written with', () => {
    expect(indentOf('{\n\t"name": "web"\n}\n')).toBe('\t');
    expect(indentOf('{\n  "name": "web"\n}\n')).toBe('  ');
  });

  it('prints the subject the workflow asks for', () => {
    expect(subject('0.3.1')).toBe('web: take @minot/ui 0.3.1 for <reason>');
  });
});
