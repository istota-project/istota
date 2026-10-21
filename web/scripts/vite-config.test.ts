import { describe, expect, it } from 'vitest';
import { spawnSync } from 'node:child_process';

// MINOT_DEV is a dev-server alias. A stale value left in the shell must not
// fail a build, and must still stop the dev server with a plain message.
// Loaded in a child process: esbuild, which loads the config, refuses the
// jsdom environment this suite runs in.
function loadAs(command: 'build' | 'serve'): { status: number | null; out: string } {
  const script = `
    const { loadConfigFromFile } = await import('vite');
    await loadConfigFromFile({ command: ${JSON.stringify(command)}, mode: 'test' }, 'vite.config.ts', undefined, 'silent');
  `;
  const run = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    cwd: process.cwd(),
    env: { ...process.env, MINOT_DEV: '/nonexistent-minot' },
    encoding: 'utf8',
    timeout: 60_000,
  });
  return { status: run.status, out: `${run.stdout}${run.stderr}` };
}

describe('MINOT_DEV pointing nowhere', () => {
  it('leaves a build alone', () => {
    expect(loadAs('build').status).toBe(0);
  });

  it('stops the dev server', () => {
    const run = loadAs('serve');
    expect(run.status).not.toBe(0);
    expect(run.out).toMatch(/no src\/lib at \/nonexistent-minot/);
  });
});
