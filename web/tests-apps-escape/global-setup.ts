// Build the kit, then every fixture, with the real Python document builder, so
// the frame the tier serves is byte for byte what an install would serve.
import { execFileSync } from 'node:child_process';
import { mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { FIXTURES, OUT, REPO, WEB, fixtureIds } from './fixtures';

export default function globalSetup() {
  execFileSync('npm', ['run', 'build:apps'], { cwd: WEB, stdio: 'inherit' });
  rmSync(OUT, { recursive: true, force: true });
  mkdirSync(OUT, { recursive: true });
  for (const id of fixtureIds()) {
    execFileSync(
      'uv',
      [
        'run',
        'python',
        'scripts/apps-escape-fixture.py',
        '--app',
        join(FIXTURES, id),
        '--out',
        join(OUT, id),
      ],
      { cwd: REPO, stdio: 'inherit' },
    );
  }
}
