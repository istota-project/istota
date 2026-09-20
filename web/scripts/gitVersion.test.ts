import { describe, expect, it } from 'vitest';
import { gitVersion } from '../gitVersion.ts';
import type { Exec } from '../gitVersion.ts';

// The failure this pins is silent in the sense that matters: `gitVersion`
// already returned 'unknown' where git is absent, and the *shell's* error
// still reached the build log because execSync inherits stderr by default.
// So the assertion is on the options the calls carry, not on the answer.

type Call = { command: string; options: Parameters<Exec>[1] };

/** Record each call and answer with `replies`, or throw where an entry is an Error. */
function recorder(...replies: (string | Error)[]): { calls: Call[]; exec: Exec } {
  const calls: Call[] = [];
  const exec: Exec = (command, options) => {
    calls.push({ command, options });
    const reply = replies[calls.length - 1] ?? '';
    if (reply instanceof Error) throw reply;
    return reply;
  };
  return { calls, exec };
}

describe('gitVersion', () => {
  it('drops the child stderr on every call it makes', () => {
    const { calls, exec } = recorder('abc1234\n', '');
    gitVersion(exec);
    expect(calls.length).toBe(2);
    for (const call of calls) {
      expect(call.options.stdio).toEqual(['ignore', 'pipe', 'ignore']);
    }
  });

  it('still reads the sha off the captured stdout', () => {
    const { calls, exec } = recorder('abc1234\n', '');
    expect(gitVersion(exec)).toBe('abc1234');
    // Capturing requires stdout stay piped — 'ignore' there would return null.
    expect(calls[0].options.stdio?.[1]).toBe('pipe');
    expect(calls[0].options.encoding).toBe('utf8');
  });

  it('marks a dirty web/ tree', () => {
    const { exec } = recorder('abc1234\n', ' M web/vite.config.ts\n');
    expect(gitVersion(exec)).toBe('abc1234-dirty');
  });

  it('answers unknown where git cannot run', () => {
    const { exec } = recorder(new Error('git: not found'));
    expect(gitVersion(exec)).toBe('unknown');
  });
});
