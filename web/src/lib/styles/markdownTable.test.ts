import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { readCascade } from './cascade';

/**
 * A markdown table's cells, and the two things that stop the columns being
 * squished to one character each (ISSUE-413).
 *
 * `.markdown table` is the standard `display: block; overflow-x: auto` scroll
 * hack, which only does something while the table's min-content width exceeds
 * its container. Two separate mechanisms decided that it never did.
 *
 * The first is inherited. `Message.svelte` sets `word-break: break-word` on
 * `.body`, which is the SAME element as `.markdown` (`<div class="body
 * markdown">`), so it reaches every cell. That value is the legacy alias for
 * `word-break: normal` + `overflow-wrap: anywhere`, and `anywhere` — unlike
 * `break-word` — reduces the intrinsic min-content size. Measured in Chrome at
 * a 320px column, the whole two-column table from the report had a min-content
 * width of 52px: one glyph per column. Nothing could ever overflow, so the
 * scroll rule was inert and auto layout handed each column a share of a phone.
 *
 * The second is auto table layout itself, and it survives the first being
 * fixed. A table pairing a short label column with a long prose column
 * distributes by content ratio, so the label column is starved however the
 * break mode is set. Resetting the break mode alone took the reported table's
 * first column from 45px to 60px at 320px — better, and still not a column.
 *
 * So the fix is both: neutralize the inherited break mode on cells, and give
 * every cell a min-width floor. Measured on the reported table at 320px, the
 * first column goes 45px -> 80px and the `Check` header stops wrapping to two
 * lines, which is the `Che`/`ck` in the screenshot.
 *
 * What is deliberately NOT the fix, because it was measured and reproduces the
 * bug: wrapping the table in a scroll container and giving it `width:
 * max-content`. Such a table needs `max-width: 100%` or a prose-bearing table
 * renders one enormous line, and once capped, auto layout starves the label
 * column exactly as before.
 *
 * These tests read the stylesheet rather than a layout, because jsdom does no
 * layout and every width above came from Chrome. They are therefore a guard
 * against the rule being removed or weakened, not a proof that it renders —
 * same standing as captions.test.ts and contentFrame.test.ts.
 */

const appCss = readCascade();

/** The body of a top-level rule, by its full selector text. */
const ruleBody = (css: string, selector: string): string | undefined => {
  const at = css.indexOf(`\n${selector} {`);
  if (at === -1) return undefined;
  return css.slice(at, css.indexOf('}', at));
};

const cellRule = () => ruleBody(appCss, '.markdown th,\n.markdown td');

describe('markdown table cells', () => {
  it('has a rule for its cells at all', () => {
    expect(cellRule()).toBeDefined();
  });

  /**
   * The premise of everything below. If this stops being true the cell reset
   * becomes belt-and-braces rather than load-bearing — harmless, but the
   * reasoning above needs revisiting rather than silently rotting.
   */
  it('is rendered inside a chat body that sets an anywhere-style break mode', () => {
    const message = readFileSync(
      join(process.cwd(), 'src/lib/components/chat/Message.svelte'),
      'utf8',
    );
    expect(message).toContain('<div class="body markdown">');
    expect(message).toMatch(/\.body\s*\{[^}]*word-break:\s*break-word/);
  });

  it('neutralizes the inherited break mode, so min-content is a word not a glyph', () => {
    const body = cellRule() ?? '';
    expect(body).toMatch(/word-break:\s*normal/);
    // `break-word`, never `anywhere`: a long unbroken token must still be able
    // to break rather than overflow, but it must not drag min-content down to
    // one character the way `anywhere` does.
    expect(body).toMatch(/overflow-wrap:\s*break-word/);
    expect(body).not.toMatch(/overflow-wrap:\s*anywhere/);
  });

  it('floors every column, which is what auto layout starves', () => {
    const body = cellRule() ?? '';
    const match = body.match(/min-width:\s*([\d.]+)ch/);
    expect(match, 'cells need a min-width floor, in ch so it tracks the type scale').toBeTruthy();
    // The value is a judgement, and it has to be measured at the app's own
    // text scale: `ch` tracks it, so 8ch is ~67px under the default 110% root
    // rather than the ~60px a browser-default page shows. 8ch is the tightest
    // floor that is not already inert (below it the longest word in a short
    // label wins anyway) and the widest that leaves a two-column table fitting
    // a 320px phone — 9ch put the reported table into a 4px scroll and 10ch
    // into a 13px one. The bound asserted here is only that the floor is a
    // column rather than a rounding error.
    expect(Number(match![1])).toBeGreaterThanOrEqual(8);
  });
});
