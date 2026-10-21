import { layerPaths, readLayer } from './cascade';
import { ROOM_COLORS } from '../roomColors';
import { describe, expect, it } from 'vitest';

// The app half of the token invariants. minot's own tokens.test.ts holds the
// library sheet's (its scales, the z ladder, the control heights, the stripe);
// this one holds the pair: minot's tokens.css as installed, plus this app's
// app-tokens.css, which adds to both theme blocks. Parity is a property of the
// pair, so it is checked over both.

const REUSABLE = readLayer('tokens');
const PRODUCT = readLayer('app-tokens');

/** The declarations inside one top-level block of one sheet, by selector. */
function declarations(css: string, selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  if (start === -1) throw new Error(`no ${selector} block`);
  const end = css.indexOf('\n}', start);
  const body = css.slice(start, end);
  const out: Record<string, string> = {};
  for (const [, name, value] of body.matchAll(/^\s*(--[\w-]+)\s*:\s*([^;]+);/gm)) {
    out[name] = value.trim();
  }
  return out;
}

/** One theme block across both token sheets. */
function block(selector: string): Record<string, string> {
  return { ...declarations(REUSABLE, selector), ...declarations(PRODUCT, selector) };
}

describe('the token split', () => {
  it('reads the reusable sheet from the installed package', () => {
    expect(layerPaths().find((p) => p.endsWith('/tokens.css'))).toMatch(
      /node_modules\/@minot\/ui\/dist\/styles\/tokens\.css$/,
    );
  });

  // A token declared in both sheets would make which value wins depend on
  // import order, which is exactly what a token layer must never decide.
  it('declares no token in both sheets', () => {
    for (const selector of [':root', ":root[data-theme='light']"]) {
      const reusable = declarations(REUSABLE, selector);
      const shared = Object.keys(declarations(PRODUCT, selector)).filter((n) => n in reusable);
      expect(shared, selector).toEqual([]);
    }
  });

  it("keeps this app's prefixes out of the library sheet", () => {
    const product =
      /^\s*--(money-|map-|chat-|room-color-|category-|scrim-pill-|accent-whatsapp|sigil-filter|setup-max)/m;
    expect(REUSABLE).not.toMatch(product);
  });

  it("aliases the app's money pair to the library's signed pair", () => {
    // The values live once, in minot's tokens.css, in both themes; the app's
    // names only point at them.
    for (const selector of [':root', ":root[data-theme='light']"]) {
      const product = declarations(PRODUCT, selector);
      expect(product['--money-income'], selector).toBe('var(--signed-pos)');
      expect(product['--money-expense'], selector).toBe('var(--signed-neg)');
    }
    expect(declarations(REUSABLE, ':root')).toHaveProperty('--signed-pos');
    expect(declarations(REUSABLE, ':root')).toHaveProperty('--signed-neg');
    expect(declarations(REUSABLE, ":root[data-theme='light']")).toHaveProperty('--signed-pos');
    expect(declarations(REUSABLE, ":root[data-theme='light']")).toHaveProperty('--signed-neg');
  });

  it('loads the app sheet straight after the whole library entry', () => {
    const order = layerPaths().map((path) => path.split('/').pop());
    expect(order.indexOf('app-tokens.css')).toBe(order.indexOf('primitives.css') + 1);
  });
});

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
    '--status-dot-success',
    '--status-dot-danger',
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
    '--signed-',
    '--money-',
    // The room-colour palette (ISSUE-433). Without this prefix the parity
    // check above simply does not see these tokens, which is the quiet failure
    // this file exists to prevent — a categorical palette is exactly the kind
    // of token somebody defines in dark and never gives a light value.
    '--room-color-',
    '--category-',
    // Interaction fills, row tints and the focused-field border. Each is a
    // colour whose name says what it is for rather than what it is, so none
    // of the prefixes above would see them.
    '--hover-',
    '--selected-',
    '--row-',
    '--focus-field-',
  ];
  const isColorToken = (name: string) =>
    COLOR_PREFIXES.some((p) => name.startsWith(p)) &&
    // The --text-* scale is half type sizes and half colors; a size is a bare
    // length. Anchored, because a loose `%` test read every hsla() as a size
    // and dropped it from the check (--status-partial-bg, --row-warn-bg).
    !/^[\d.]+(rem|px|em|%)?$/.test(dark[name] ?? '');

  /**
   * A `filter` that corrects an asset the theme does not control — the octopus
   * sigil, the browser's calendar glyph. Not a colour by prefix, but it fails
   * exactly the same way: define it in dark only and the light theme renders
   * an inverted mark it never asked for.
   *
   * The prefix list missed both of these. `--sigil-filter` had carried the gap
   * since it was introduced; `--calendar-icon-filter` would have inherited it.
   */
  const isThemeFilter = (name: string) => name.endsWith('-filter');

  const needsBothThemes = (name: string) => isColorToken(name) || isThemeFilter(name);

  it('every color token has a light-theme value', () => {
    const missing = Object.keys(dark).filter(
      (name) => needsBothThemes(name) && !THEME_INVARIANT.has(name) && !(name in light),
    );
    expect(missing).toEqual([]);
  });

  it('covers the theme-correcting filters, which are not colors by name', () => {
    // Guards the guard: if the prefix list or the suffix test stops matching
    // these, the parity check above goes quiet rather than failing, which is
    // the failure mode this whole file exists to prevent.
    const filters = Object.keys(dark).filter(isThemeFilter);
    expect(filters).toContain('--sigil-filter');
    expect(filters).toContain('--calendar-icon-filter');
    for (const name of filters) expect(light, `${name} needs a light value`).toHaveProperty(name);
  });

  it('the light theme defines no token the dark theme lacks', () => {
    // A light-only token renders as nothing in dark, the same bug mirrored.
    expect(Object.keys(light).filter((name) => !(name in dark))).toEqual([]);
  });

  it('covers the room-colour palette, which is what the prefix was added for', () => {
    // Guards the guard, the way the filter test above does: the parity check
    // is a filter over a prefix list, so a palette the list stops matching
    // goes quiet rather than failing. ROOM_COLORS is the palette itself, so
    // this also catches a name added there with no token behind it.
    const names = Object.keys(dark).filter((n) => n.startsWith('--room-color-'));
    expect(names.length).toBe(ROOM_COLORS.length);
    for (const c of ROOM_COLORS) {
      expect(dark, `--room-color-${c} needs a dark value`).toHaveProperty(`--room-color-${c}`);
      expect(light, `--room-color-${c} needs a light value`).toHaveProperty(`--room-color-${c}`);
    }
  });

  it('covers the role-named colour tokens, which no colour prefix names', () => {
    // Guards the guard again: drop one of these prefixes from the list and
    // the parity check stops seeing the token rather than failing on it.
    for (const name of [
      '--hover-bg',
      '--hover-fg',
      '--selected-bg',
      '--row-danger-bg',
      '--row-warn-bg',
      '--focus-field-border',
    ]) {
      expect(needsBothThemes(name), `${name} is not parity-checked`).toBe(true);
      expect(light, `${name} needs a light value`).toHaveProperty(name);
    }
  });

  it('gives each room colour a distinct value within a theme', () => {
    // Two names resolving to one hue is a picker offering the same swatch
    // twice, which reads as a rendering bug rather than a palette mistake.
    for (const [label, blockValues] of [
      ['dark', dark],
      ['light', light],
    ] as const) {
      const values = ROOM_COLORS.map((c) => blockValues[`--room-color-${c}`]);
      expect(new Set(values).size, `${label} has a duplicate room colour`).toBe(values.length);
    }
  });
});
