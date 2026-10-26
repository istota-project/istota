// The app fixtures the escape tier frames: every folder under
// tests/fixtures/apps/ holding both a manifest.toml and an index.html. A folder
// with only a README (the hostile fixture before anyone has written it) is not
// one, so the tier runs on whatever is there.
import { existsSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';

export const WEB = resolve(import.meta.dirname, '..');
export const REPO = resolve(WEB, '..');
export const FIXTURES = join(REPO, 'tests/fixtures/apps');
export const OUT = join(import.meta.dirname, '.out');
export const PORT = 5199;
/** The recorder's own origin: where every fixture points the requests it attempts. */
export const RECORDER_PORT = 5198;
export const RECORDER = `http://127.0.0.1:${RECORDER_PORT}`;
export const CONTRACT = 'escape-contract';
export const HOSTILE = 'hostile';
export const EARLY_NAV = 'early-nav';

export function fixtureIds(): string[] {
  if (!existsSync(FIXTURES)) return [];
  return readdirSync(FIXTURES, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .filter(
      (id) =>
        existsSync(join(FIXTURES, id, 'manifest.toml')) &&
        existsSync(join(FIXTURES, id, 'index.html')),
    )
    .sort();
}

/** Set by the negative control: the frame is served without its CSP header. */
export function cspRemoved(): boolean {
  return process.env.APPS_ESCAPE_NO_CSP === '1';
}
