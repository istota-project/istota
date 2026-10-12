/**
 * The page's step order against the server's.
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
import { SETUP_STEPS, isSetupStep, stepPosition } from './setupDraft.svelte';

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
