import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The money record-table shell — the toolbar/list/header/row rules every money
 * list page inherits, defined once in this layout.
 *
 * Same reasoning as controlTier.test.ts: these are inherited-token and
 * reserved-height invariants that are invisible at any one call site, so a page
 * can silently opt out of them and nothing fails until someone eyeballs two
 * tabs side by side.
 */

const layout = readFileSync(join(process.cwd(), 'src/routes/money/+layout.svelte'), 'utf8');

/** The declarations inside a `:global(<selector>)` rule. */
const ruleBody = (selector: string): string => {
  const start = layout.indexOf(`:global(${selector}) {`);
  if (start === -1) throw new Error(`no :global(${selector}) rule in the money layout`);
  return layout.slice(start, layout.indexOf('}', start));
};

describe('.money-result-count', () => {
  // The count's own line box is shorter than the tier, so left to itself its
  // position is decided by whatever else is in the bar: on a toolbar that fits
  // on one line the min-height's leftover is split above and below it, and on
  // one that wraps there is no leftover and it sits flush on the padding — a
  // ~6px jump between two pages that look identical otherwise.
  it('reserves the field-tier height so its position does not depend on its siblings', () => {
    expect(ruleBody('.money-result-count')).toMatch(/min-height:\s*var\(--control-height-lg\)/);
  });

  it('centres its own text in that reserved box', () => {
    // min-height alone would only make the box taller and leave the text at the
    // top of it, which is the same defect pointing the other way.
    const body = ruleBody('.money-result-count');
    expect(body).toMatch(/display:\s*inline-flex/);
    expect(body).toMatch(/align-items:\s*center/);
  });
});

describe('.money-toolbar', () => {
  it('reserves a control row whether or not it holds controls', () => {
    // What makes a count-only bar the same height as one with filters, so the
    // table under it starts at the same place on every tab.
    expect(ruleBody('.money-toolbar')).toMatch(
      /min-height:\s*calc\(var\(--control-height-lg\)\s*\+\s*var\(--space-4\)\)/,
    );
  });
});
