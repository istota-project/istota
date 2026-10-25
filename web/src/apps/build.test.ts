import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it } from 'vitest';
import { checkInlineable, KIT_SCRIPTS, manifestOf, starterFiles } from './build';

const KIT = join(process.cwd(), 'src', 'apps', 'kit');
const made: string[] = [];

function scratch(): string {
  const dir = mkdtempSync(join(tmpdir(), 'apps-build-'));
  made.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of made.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('the apps build', () => {
  it('passes the kit sources as they are', () => {
    for (const name of [...KIT_SCRIPTS, 'istota.css']) {
      expect(() => checkInlineable(name, readFileSync(join(KIT, name), 'utf8'))).not.toThrow();
    }
  });

  it('refuses a script or stylesheet that would end its element early', () => {
    expect(() => checkInlineable('a.js', 'const s = "</SCRIPT>";')).toThrow(/<\/script/);
    expect(() => checkInlineable('a.js', 'x <!-- y')).toThrow(/<!--/);
    expect(() => checkInlineable('a.css', 'a::after{content:"</style>"}')).toThrow(/<\/style/);
    expect(() => checkInlineable('a.css', '/* <!-- is fine in CSS */')).not.toThrow();
  });

  it('copies every starter file but the tests, and none when there are no starters', () => {
    const dir = scratch();
    expect(starterFiles(join(dir, 'missing'))).toEqual([]);
    mkdirSync(join(dir, 'board', 'lib'), { recursive: true });
    writeFileSync(join(dir, 'board', 'manifest.toml'), 'id = "board"\n');
    writeFileSync(join(dir, 'board', 'index.html'), '<!doctype html>');
    writeFileSync(join(dir, 'board', 'lib', 'board-model.js'), '');
    writeFileSync(join(dir, 'board', 'board-model.test.ts'), '');
    expect(starterFiles(dir)).toEqual([
      'board/index.html',
      'board/lib/board-model.js',
      'board/manifest.toml',
    ]);
  });

  it('refuses a symlink among the starter files', () => {
    const dir = scratch();
    mkdirSync(join(dir, 'board'));
    symlinkSync('/etc/hosts', join(dir, 'board', 'hosts'));
    expect(() => starterFiles(dir)).toThrow(/symlink/);
  });

  it('hashes every output file but the manifest, keyed by its path', () => {
    const dir = scratch();
    mkdirSync(join(dir, 'kit'));
    writeFileSync(join(dir, 'kit', 'guard.js'), 'a');
    writeFileSync(join(dir, 'manifest.json'), '{}');
    expect(manifestOf(dir)).toEqual({
      'kit/guard.js': createHash('sha256').update('a').digest('hex'),
    });
  });
});
