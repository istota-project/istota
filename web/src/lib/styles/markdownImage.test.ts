import { describe, it, expect } from 'vitest';
import { readCascade } from './cascade';

/**
 * The inline chat image, drawn in minot's embed box. The box itself (`.embed`
 * and its two tokens) is minot's and minot's embed test pins it; this holds the
 * chat image's half.
 *
 * Every embed in a reading column (an inline image, a location map, and any
 * later video or PDF page) is sized by one rule, `.embed` in primitives.css,
 * against two tokens: `--embed-max-width` and `--embed-max-height`. The box
 * used to be two unrelated rules: images capped at `50vh` on desktop only, so
 * their size followed the window's height, and the location map at 2:1 on
 * desktop and 4:3 on a phone across the whole column.
 *
 * The height cap is stated as a bound on WIDTH through `--embed-ratio`. A
 * `#w=&h=` hint emits `width`/`height` attributes, which make the used width
 * specified, so a `max-height` alone clamps the height while the width stays:
 * the picture stretched x1.23 on an 800x600 capture and x9.68 on an 800x4000
 * one, with correct hints. The bound leaves `height: auto` free to follow.
 *
 * These read the stylesheet rather than a layout, because jsdom does no
 * layout: they guard the rules against being removed or weakened, and are not
 * a proof that they render. Same standing as markdownTable.test.ts.
 */

const appCss = readCascade();

/** The body of a top-level rule, by its full selector text. */
function ruleBody(css: string, selector: string): string | undefined {
  const at = css.indexOf(`\n${selector} {`);
  if (at === -1) return undefined;
  return css.slice(at, css.indexOf('}', at));
}

describe('inline chat image and the embed box', () => {
  it('applies at every width, with no breakpoint of its own', () => {
    // minot pins `.embed` itself; the chat image rides on it here.
    for (const m of appCss.matchAll(/@media[^{]*\{([\s\S]*?)\n\}/g)) {
      expect(m[1]).not.toMatch(/\.md-image\s*\{/);
    }
  });
});

describe('inline chat image', () => {
  it('has a rule at all', () => {
    expect(ruleBody(appCss, '.markdown .md-image')).toBeDefined();
  });

  /**
   * Sizing is the embed box's alone. A `max-width` here would outrank `.embed`
   * on specificity and drop the box's caps without a sound.
   */
  it('leaves sizing to the embed box', () => {
    const body = ruleBody(appCss, '.markdown .md-image') ?? '';
    expect(body).not.toMatch(/max-width|max-height/);
  });

  /** The premise of the height bound: the browser may still follow the ratio. */
  it('leaves the browser free to preserve the aspect ratio', () => {
    const body = ruleBody(appCss, '.markdown .md-image') ?? '';
    expect(body).toMatch(/height:\s*auto/);
    expect(body).not.toMatch(/^\s*width:/m);
  });

  /** The floor under the geometry: a wrong hint costs empty space, never a squash. */
  it('never lets an image be distorted, whatever the box does', () => {
    const body = ruleBody(appCss, '.markdown .md-image') ?? '';
    expect(body).toMatch(/object-fit:\s*contain/);
  });

  /**
   * The reserved box has to look like something, or it reads as a gap in the
   * layout rather than as a picture on its way. Its own token, given a value in
   * both themes, since the step is judged against `--surface-reading`.
   */
  it('fills the reserved box with the media placeholder surface', () => {
    const body = ruleBody(appCss, '.markdown .md-image') ?? '';
    expect(body).toMatch(/background-color:\s*var\(--surface-media-placeholder\)/);
    expect(appCss.match(/--surface-media-placeholder:/g)?.length ?? 0).toBe(2);
  });
});
