/**
 * The page's step order against the server's, and the username the Welcome
 * screen suggests against the login's own rule.
 *
 * `/validate` takes a step name and the step-to-fields map lives in Python
 * (`SETUP_STEPS` in `webui/setup_app.py`), so the two cannot disagree about
 * which fields a screen owns. They can still disagree about which screens
 * exist: a step the page names that the server does not know is a 400 on
 * every Next. This reads the server's keys out of its source and holds the
 * page's list to them, order included.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { SETUP_STEPS, deriveUsername, isSetupStep, stepPosition } from './setupDraft.svelte';

const SETUP_APP = resolve(__dirname, '../../../../../src/istota/webui/setup_app.py');

function serverSteps(): string[] {
  const source = readFileSync(SETUP_APP, 'utf-8');
  const start = source.indexOf('SETUP_STEPS: dict[str, tuple[str, ...]] = {');
  const end = source.indexOf('\n}\n', start);
  expect(start).toBeGreaterThan(-1);
  const block = source.slice(start, end);
  const steps = [...block.matchAll(/^ {4}"([a-z_]+)":/gm)].map((m) => m[1]);
  // The review screen is added after the literal, as the union of the rest.
  const extra = [...source.matchAll(/^SETUP_STEPS\["([a-z_]+)"\]\s*=/gm)].map((m) => m[1]);
  return [...steps, ...extra];
}

describe('setup steps', () => {
  it('are the server’s steps, in the server’s order', () => {
    expect(SETUP_STEPS.map((s) => s.id)).toEqual(serverSteps());
  });

  it('recognise only their own ids', () => {
    expect(isSetupStep('model')).toBe(true);
    expect(isSetupStep('unlock')).toBe(false);
    expect(isSetupStep('')).toBe(false);
  });

  it('number from one', () => {
    expect(stepPosition('welcome')).toBe(1);
    expect(stepPosition('review')).toBe(SETUP_STEPS.length);
  });
});

const AUTH = resolve(__dirname, '../../../../../src/istota/webui/auth.py');

/** `valid_new_user_id`'s pattern, read out of the server's source. */
function loginUserIdPattern(): RegExp {
  const source = readFileSync(AUTH, 'utf-8');
  const match = source.match(/def valid_new_user_id[\s\S]*?re\.fullmatch\(r"([^"]+)"/);
  expect(match).not.toBeNull();
  return new RegExp(`^(?:${match![1]})$`);
}

describe('the username suggested from an email', () => {
  it.each([
    ['ada@example.com', 'ada'],
    ['Ada.Lovelace@Example.com', 'ada.lovelace'],
    ['ada+setup@example.com', 'adasetup'],
    ['  grace_h-1@example.com ', 'grace_h-1'],
    ['a'.repeat(40) + '@example.com', 'a'.repeat(32)],
    ['_ada@example.com', 'admin'],
    ['élodie@example.com', 'lodie'],
    ['+++@example.com', 'admin'],
    ['', 'admin'],
  ])('%s gives %s', (email, expected) => {
    expect(deriveUsername(email)).toBe(expected);
  });

  it('always passes the rule the login applies', () => {
    const pattern = loginUserIdPattern();
    for (const email of [
      'Ada.Lovelace@example.com',
      'x'.repeat(50),
      '-ada@x',
      'ÅSA@x',
      '.@x',
      '',
    ]) {
      expect(deriveUsername(email)).toMatch(pattern);
    }
  });
});
