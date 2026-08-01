import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * `.content-frame` — the wide-dashboard content column: a cap, centred in
 * whatever pane it was given.
 *
 * This started as four declarations written privately in the health layout,
 * and the second module that wanted the same column (money's portfolio) could
 * only get it by writing them out again — the shape that produced four
 * record-table implementations before the money shell, and three admin pages
 * each capping at their own 1100px. So the point of these tests is not that
 * the CSS parses: it is that the cap stays in ONE place, and that a module
 * adopting the column reads it rather than restating it.
 *
 * Same reasoning as controlTier.test.ts. A page that hardcodes its own
 * max-width silently opts out of a shared column it looks like it belongs to,
 * and nothing about the rendered page says so.
 */

const appCss = readFileSync(join(process.cwd(), 'src/app.css'), 'utf8');
const healthLayout = readFileSync(join(process.cwd(), 'src/routes/health/+layout.svelte'), 'utf8');
const portfolioLayout = readFileSync(
  join(process.cwd(), 'src/routes/money/portfolio/+layout.svelte'),
  'utf8',
);
const reportsLayout = readFileSync(
  join(process.cwd(), 'src/routes/money/reports/+layout.svelte'),
  'utf8',
);

/** Every layout that adopts the column, so a new one is added in one place. */
const adopters = [
  ['health/+layout.svelte', healthLayout],
  ['money/portfolio/+layout.svelte', portfolioLayout],
  ['money/reports/+layout.svelte', reportsLayout],
] as const;

/**
 * Markup with comments blanked out. These layouts explain in prose *why* the
 * header is not framed, so a naive text scan finds `content-frame` in the
 * sentence saying it should not be there and reports the opposite of the
 * truth. Blanked rather than deleted, so offsets still line up.
 */
const markupOnly = (source: string): string =>
  source.replace(/<!--[\s\S]*?-->/g, (m) => ' '.repeat(m.length));

/** The `:root` block — the token roster, excluding the theme and media blocks. */
const rootBlock = appCss.slice(appCss.indexOf(':root {'), appCss.indexOf('*,\n*::before'));

const tokenValue = (css: string, name: string): string | undefined =>
  css.match(new RegExp(`${name}:\\s*([^;]+);`))?.[1]?.trim();

/** The body of a rule, by selector. */
const ruleBody = (css: string, selector: string): string | undefined => {
  const at = css.indexOf(`\n${selector} {`);
  if (at === -1) return undefined;
  return css.slice(at, css.indexOf('}', at));
};

describe('--content-max', () => {
  it('is defined once, in px', () => {
    const value = tokenValue(rootBlock, '--content-max');
    // px rather than rem on purpose: this bounds the *screen* a layout may
    // claim, not the type inside it. A rem cap would widen the column at the
    // large text-scale setting — the setting picked to make lines easier to
    // follow.
    expect(value).toMatch(/^\d+px$/);
  });

  it('is the only content cap — no module restates it', () => {
    // The failure this guards is a second module capping at its own number and
    // the two drifting apart, which is invisible until you put the two pages
    // side by side on a wide monitor.
    const raw = tokenValue(rootBlock, '--content-max') ?? '';
    for (const [name, source] of adopters) {
      expect(source, `${name} restates the cap instead of reading --content-max`).not.toContain(
        raw,
      );
    }
  });
});

describe('.content-frame', () => {
  const body = ruleBody(appCss, '.content-frame');

  it('caps at the token and centres', () => {
    expect(body).toBeDefined();
    expect(body).toContain('max-width: var(--content-max)');
    expect(body).toContain('margin-inline: auto');
  });

  it('is a growing flex column', () => {
    // `.center-msg` centres itself with `flex: 1`, which needs an unbroken
    // chain of growing flex columns up to `.shell-main` — inserting a plain
    // block here would strand every framed page's loading and empty state at
    // the top of the pane. `flex-basis: auto` and no shrink, so content taller
    // than the pane still extends the scroll area.
    expect(body).toContain('flex-direction: column');
    expect(body).toMatch(/flex:\s*1\s+0\s+auto/);
  });

  it('carries no padding of its own', () => {
    // Geometry only. Health pads the frame itself; the money pages pad their
    // own sections, and a value baked in here would land on top of theirs.
    // Spacing stays at the call site — the division `.micro-label` draws.
    expect(body).not.toMatch(/^\s*padding/m);
  });
});

describe('adopters', () => {
  it('health composes the frame onto its module shell', () => {
    // The class has to stay on a real element in the layout file: the
    // `:global()` module rules are scoped under `.health-frame`, and Svelte
    // prunes a selector whose subject it can no longer see in the markup —
    // silently, which is how two rules died in the health migration.
    expect(healthLayout).toContain('class="content-frame health-frame"');
  });

  // The bar is section chrome and spans the pane; the frame caps the content
  // inside it, the way health's app bar already sits over its framed column.
  // Both money sections are shaped the same way, so the check is one loop —
  // framing the header instead puts the nav 0.75rem left of every column
  // beneath it, because a padded bar's auto margins absorb its padding along
  // with the rest of the free space.
  for (const [name, source] of [
    ['portfolio', portfolioLayout],
    ['reports', reportsLayout],
  ] as const) {
    it(`money/${name} frames the section body but not the section header`, () => {
      const markup = markupOnly(source);
      const header = markup.indexOf('money-section-header');
      const bodyAt = markup.indexOf('money-section-body');
      const frameAt = markup.indexOf('content-frame', bodyAt);

      expect(header).toBeGreaterThan(-1);
      expect(bodyAt).toBeGreaterThan(-1);
      // The frame is *inside* the scroller, so the scrollbar stays at the
      // pane's edge where every other money section puts it.
      expect(frameAt).toBeGreaterThan(bodyAt);
      expect(markup.slice(header, bodyAt)).not.toContain('content-frame');
    });
  }
});
