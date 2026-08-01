import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';

/**
 * iOS renders a date input with no value as *nothing* — there is no
 * `mm/dd/yyyy` hint the way desktop has one. `app.css` drops the platform
 * appearance on temporal inputs (the native control's intrinsic width ignored
 * `max-width` and overflowed cards on an iPhone), which means their box is
 * content-sized in both axes — so an unset date collapses to a blank sliver.
 * The vertical half of that was already caught and pinned with a `min-height`
 * on `::-webkit-date-and-time-value`; the horizontal half shipped, and the
 * health-history range filter rendered as two empty narrow boxes.
 *
 * These tests hold both halves: app.css floors the width without letting the
 * overflow back in, and no stylesheet re-declares `min-width` on a temporal
 * input, which is what defeated the floor the first time (a page rule outranks
 * a bare type selector, and one restated `min-width: 0`).
 */

const SRC = resolve(process.cwd(), 'src');
const css = readFileSync(join(SRC, 'app.css'), 'utf8');

const TEMPORAL_TYPES = ['date', 'datetime-local', 'time', 'month', 'week'];

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** Body of the first rule whose selector list contains `needle`. */
function ruleBodyContaining(source: string, needle: string): string | null {
  for (const m of stripComments(source).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (m[1].includes(needle)) return m[2];
  }
  return null;
}

function declaration(body: string, prop: string): string | null {
  const m = new RegExp(`(?:^|[;{\\s])${prop}\\s*:\\s*([^;}]+)`).exec(body);
  return m ? m[1].trim() : null;
}

/** Every `.svelte` / `.css` file under src, so a rule added anywhere is seen. */
function stylesheets(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) stylesheets(path, out);
    else if (/\.(svelte|css)$/.test(entry.name)) out.push(path);
  }
  return out;
}

const appearanceRule = ruleBodyContaining(css, `input[type='date']`);
const valuePseudoRule = ruleBodyContaining(css, `::-webkit-date-and-time-value`);

describe('app.css temporal input sizing', () => {
  it('parses the rules it asserts on', () => {
    expect(appearanceRule, 'no rule in app.css targets a date input').not.toBeNull();
    expect(valuePseudoRule, 'no rule targets ::-webkit-date-and-time-value').not.toBeNull();
    // The floor only matters because the appearance is dropped; if that ever
    // goes away these assertions are guarding a rendering that no longer exists.
    expect(declaration(appearanceRule!, 'appearance')).toBe('none');
  });

  it('covers every temporal input type in both rules', () => {
    // A type left out of either selector list gets the collapse back on its own.
    for (const type of TEMPORAL_TYPES) {
      const selectors = stripComments(css);
      expect(selectors, `${type} missing the appearance reset`).toContain(
        `input[type='${type}'] {`.replace(' {', ''),
      );
      expect(selectors, `${type} missing the value pseudo-element rule`).toContain(
        `input[type='${type}']::-webkit-date-and-time-value`,
      );
    }
  });

  it('floors the width instead of letting an empty input collapse', () => {
    const minWidth = declaration(appearanceRule!, 'min-width');
    expect(minWidth).not.toBeNull();
    // `0` is the specific value that shipped the bug: it licenses the flex
    // shrink that takes an empty, content-sized input down to its padding.
    expect(minWidth).not.toMatch(/^0(px|rem|em|%)?$/);
  });

  it('caps that floor at the container so the overflow cannot return', () => {
    // The reason this block exists is a date input overflowing a narrow card on
    // iOS. A floor written as a bare length reintroduces exactly that wherever
    // the container is narrower than the floor, so it has to be capped.
    const minWidth = declaration(appearanceRule!, 'min-width')!;
    expect(minWidth).toMatch(/min\(/);
    expect(minWidth).toContain('100%');
    expect(declaration(appearanceRule!, 'max-width')).toBe('100%');
  });

  it('keeps the height floor that the same collapse produced vertically', () => {
    expect(declaration(valuePseudoRule!, 'min-height')).not.toBeNull();
    expect(declaration(valuePseudoRule!, 'text-align')).toBe('left');
  });
});

describe('no stylesheet overrides the temporal input floor', () => {
  const offenders: string[] = [];
  for (const file of stylesheets(SRC)) {
    if (file.endsWith('app.css')) continue;
    for (const m of stripComments(readFileSync(file, 'utf8')).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selector = m[1].replace(/\s+/g, ' ').trim();
      const targetsTemporal = TEMPORAL_TYPES.some((t) => selector.includes(`input[type='${t}']`));
      // A pseudo-element rule (the calendar picker icon) styles the shadow
      // child, not the input's own box, so it cannot reach the floor.
      if (!targetsTemporal || selector.includes('::')) continue;
      if (declaration(m[2], 'min-width') !== null) {
        offenders.push(`${file.slice(SRC.length + 1)}: ${selector}`);
      }
    }
  }

  it('leaves min-width to app.css', () => {
    // Any page rule outranks app.css's bare type selector, so restating
    // min-width here silently defeats the floor — which is how the
    // health-history filter shipped as two blank boxes.
    expect(offenders).toEqual([]);
  });
});
