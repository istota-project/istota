import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The field tier — the bigger, squarer controls a body form/toolbar uses, as
 * opposed to the compact pill chrome in the app bar.
 *
 * The whole mechanism is inherited custom properties: a `.control-row`
 * container redefines the height and radius tokens, and every control inside
 * resolves them. That only works while the controls actually *read* the
 * channel, which is invisible at any one call site and easy to undo by writing
 * `--radius-pill` back into a component — hence these tests rather than a note.
 *
 * Same reasoning as touchZoom.test.ts: a control that hardcodes its own box
 * silently opts out of a tier it looks like it belongs to.
 */

const uiDir = join(process.cwd(), 'src/lib/components/ui');
const appCss = readFileSync(join(process.cwd(), 'src/app.css'), 'utf8');

/** The `:root` block — the token roster, excluding the theme and media blocks. */
const rootBlock = appCss.slice(appCss.indexOf(':root {'), appCss.indexOf('*,\n*::before'));

const tokenValue = (css: string, name: string): string | undefined =>
  css.match(new RegExp(`${name}:\\s*([^;]+);`))?.[1]?.trim();

const remValue = (raw: string | undefined): number => {
  const m = raw?.match(/^([\d.]+)rem$/);
  return m ? Number(m[1]) : NaN;
};

describe('field-tier tokens', () => {
  it('defines --control-height-lg above the compact tiers, in rem', () => {
    const sm = remValue(tokenValue(rootBlock, '--control-height-sm'));
    const md = remValue(tokenValue(rootBlock, '--control-height-md'));
    const lg = remValue(tokenValue(rootBlock, '--control-height-lg'));

    // rem so the tier tracks the text-scale preference, like the other two.
    expect(lg).not.toBeNaN();
    expect(lg).toBeGreaterThan(md);
    expect(md).toBeGreaterThan(sm);
  });

  it('defines --control-radius, defaulting to the compact pill', () => {
    // Root default is the app bar's shape, so adopting the channel in a
    // component is a no-op everywhere outside a .control-row.
    expect(tokenValue(rootBlock, '--control-radius')).toBe('var(--radius-pill)');
  });

  it('floors the field height on coarse pointers', () => {
    // iOS floors a focused text input at 16px, so an input in a mixed row grows
    // on touch while the buttons beside it do not. The tier has to rise with
    // it or the row lands back where it started — the bug this exists to fix.
    const coarse = appCss.slice(appCss.indexOf('@media (pointer: coarse)'));
    expect(coarse).toMatch(/--control-height-lg:\s*max\(/);
  });
});

describe('.control-row', () => {
  const row = appCss.slice(appCss.indexOf('.control-row {'));

  it('collapses both compact tiers onto the field height', () => {
    // Both, not just md: a row mixes a default Button (md) with a Select whose
    // default size is sm, and leaving sm alone would keep the mismatch.
    expect(row).toMatch(/--control-height-sm:\s*var\(--control-height-lg\)/);
    expect(row).toMatch(/--control-height-md:\s*var\(--control-height-lg\)/);
  });

  it('squares off the control radius', () => {
    expect(row).toMatch(/--control-radius:\s*var\(--radius-sm\)/);
  });
});

/** The controls a `.control-row` is expected to lift in step. */
const TIER_CONTROLS = [
  'Button.svelte',
  'Chip.svelte',
  'Input.svelte',
  'IconButton.svelte',
  'Select.svelte',
];

describe('controls resolve the tier channel', () => {
  const read = (file: string) => readFileSync(join(uiDir, file), 'utf8');

  /** Corner-shape declarations, minus the ones that are deliberately fixed. */
  const radiiOf = (css: string) =>
    [...css.matchAll(/border-radius:\s*([^;]+);/g)].map((m) => m[1].trim());

  it.each([
    ['Button.svelte', '.btn'],
    ['Chip.svelte', '.chip'],
  ])('%s takes its corner from --control-radius', (file) => {
    const radii = radiiOf(read(file));
    expect(radii).toContain('var(--control-radius)');
    // A leftover pill would out-live the token and pin the shape.
    expect(radii).not.toContain('var(--radius-pill)');
  });

  it('the Select trigger takes its corner from --control-radius', () => {
    const css = read('Select.svelte');
    const trigger = css.slice(css.indexOf('.ui-select-trigger)'), css.indexOf('--sm)'));
    expect(trigger).toMatch(/border-radius:\s*var\(--control-radius\)/);
  });

  it.each([
    ['Button.svelte', /min-height:\s*var\(--control-height-(sm|md)\)/],
    ['Chip.svelte', /min-height:\s*var\(--control-height-(sm|md)\)/],
    ['Input.svelte', /min-height:\s*var\(--control-height-md\)/],
    ['IconButton.svelte', /min-height:\s*var\(--control-height-md\)/],
  ])('%s reserves a height through the channel', (file, pattern) => {
    expect(read(file)).toMatch(pattern);
  });

  it.each([['Input.svelte'], ['Field.svelte']])(
    '%s pins leading wherever it declares the font shorthand',
    (file) => {
      // `font: inherit` also sets line-height, pulling in the body's 1.5 — and
      // at a specificity no weightless rule outside the component can correct.
      // Left alone, a 16px-floored input computes taller than the tier around
      // it and stands proud of every control beside it on touch, which is the
      // whole defect. Anything declaring the shorthand has to pin the leading
      // too, in the same file.
      const css = read(file);
      expect(css).toMatch(/font:\s*inherit/);
      expect(css).toMatch(/line-height:\s*1\.2/);
    },
  );

  it('leaves the full-width Select trigger on the shared leading', () => {
    // It used to pin 1.5 to chase the input's inherited leading. Both now sit
    // at 1.2 with the same padding and min-height, so a second value here
    // would only reintroduce the mismatch it was compensating for.
    const css = read('Select.svelte');
    expect(css).not.toMatch(/line-height:\s*1\.5/);
  });

  it('leaves no tier control sizing its own box in px', () => {
    // A px height cannot track the text scale and cannot be raised by the
    // container, so it is the one way to sit out the tier entirely.
    //
    // Scoped to the controls that participate rather than all of ui/: an
    // out-of-flow ::before touch-target overlay is legitimately 44px (see the
    // touch-target note in web/AGENTS.md), and it sizes a hit area rather than
    // a box in the row.
    const offenders = TIER_CONTROLS.filter((f) => /(^|\n)\s*(min-)?height:\s*\d+px/.test(read(f)));
    expect(offenders).toEqual([]);
  });
});
