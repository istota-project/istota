import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// app.css is the token home, so the invariants that make the tokens usable have
// to be asserted here rather than inferred from the pages that consume them.
// Read from the project root: vitest serves this file over a vite-style URL, so
// import.meta.url is not a file: URL here.

const APP_CSS = readFileSync(resolve(process.cwd(), 'src/app.css'), 'utf8');

/** The declarations inside one top-level block, by selector. */
function block(selector: string): Record<string, string> {
  const start = APP_CSS.indexOf(`${selector} {`);
  if (start === -1) throw new Error(`no ${selector} block in app.css`);
  const end = APP_CSS.indexOf('\n}', start);
  const body = APP_CSS.slice(start, end);
  const out: Record<string, string> = {};
  for (const [, name, value] of body.matchAll(/^\s*(--[\w-]+)\s*:\s*([^;]+);/gm)) {
    out[name] = value.trim();
  }
  return out;
}

const dark = block(':root');
const light = block(":root[data-theme='light']");

describe('theme parity', () => {
  // The anti-pattern AGENTS.md names: a color defined once, in dark, then
  // rendered as a dark fill on white. A color token without a light value is
  // that bug waiting to be noticed.
  const THEME_INVARIANT = new Set([
    // Deliberately one value in both themes: these sit on a surface the theme
    // does not control, so flipping them would put dark text on a dark scrim.
    '--on-accent-fg',
    '--on-scrim-fg',
    '--scrim-bg',
    // A solid amber fill sets its own text color, so it needs none of the
    // darkening --accent-amber gets so amber *text* passes on white.
    '--accent-amber-fill',
    '--accent-amber-fill-hover',
    '--accent-amber-fill-fg',
    '--status-dot-ok',
    '--status-dot-bad',
    '--status-dot-warn',
    '--status-dot-info',
    '--status-critical-fg',
  ]);

  const COLOR_PREFIXES = [
    '--status-',
    '--surface-',
    '--text-',
    '--border-',
    '--accent',
    '--money-',
  ];
  const isColorToken = (name: string) =>
    COLOR_PREFIXES.some((p) => name.startsWith(p)) &&
    // The --text-* scale is half type sizes and half colors; sizes are rem.
    !/^\d|rem|px|%/.test(dark[name] ?? '');

  it('every color token has a light-theme value', () => {
    const missing = Object.keys(dark).filter(
      (name) => isColorToken(name) && !THEME_INVARIANT.has(name) && !(name in light),
    );
    expect(missing).toEqual([]);
  });

  it('the light theme defines no token the dark theme lacks', () => {
    // A light-only token renders as nothing in dark, the same bug mirrored.
    expect(Object.keys(light).filter((name) => !(name in dark))).toEqual([]);
  });
});

describe('z-index scale', () => {
  const z = (name: string) => {
    const raw = dark[name];
    expect(raw, `${name} is not defined`).toBeDefined();
    return Number(raw);
  };

  it('is strictly ordered bottom to top', () => {
    const ladder = [
      '--z-sticky',
      '--z-drawer-backdrop',
      '--z-drawer',
      '--z-notice',
      '--z-modal',
      '--z-modal-panel',
      '--z-viewer',
      '--z-viewer-control',
      '--z-lightbox',
      '--z-popover',
      '--z-toast',
    ];
    const values = ladder.map(z);
    expect(values).toEqual([...values].sort((a, b) => a - b));
    expect(new Set(values).size).toBe(values.length);
  });

  it('puts a popover above a dialog panel', () => {
    // Not theoretical: every money form is a Select inside a Modal, and both
    // portal to <body>, so the two values compete in the root stacking context.
    expect(z('--z-popover')).toBeGreaterThan(z('--z-modal-panel'));
  });

  it('puts the lightbox above the reader it opens from', () => {
    expect(z('--z-lightbox')).toBeGreaterThan(z('--z-viewer-control'));
  });
});
