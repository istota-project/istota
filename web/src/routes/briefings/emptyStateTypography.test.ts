import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * The reader's empty state — "No briefings yet", plus the line pointing at
 * settings — sized itself outside the type scale: the h1 at a literal 1.1rem
 * and the paragraph inheriting the 1rem body default, against a real briefing
 * body one rule above it at `--text-base` (0.85rem). So the screen a user meets
 * before their first briefing ever runs was the one screen rendering a step
 * larger than the rest of the app.
 *
 * Same reasoning as NoticeBanner.typography.test.ts: a size written as a
 * literal is one nothing else can read, so it drifts alone.
 */

const source = readFileSync(resolve(process.cwd(), 'src/routes/briefings/+page.svelte'), 'utf8');

const stripComments = (css: string): string => css.replace(/\/\*[\s\S]*?\*\//g, '');

/** The `<style>` body of a Svelte file. */
const styleBlock = (src: string): string => {
  const open = src.indexOf('<style');
  if (open === -1) return '';
  return src.slice(src.indexOf('>', open) + 1, src.lastIndexOf('</style>'));
};

/** Flat `selector { body }` rules. Comments first, since several quote a brace. */
function rules(css: string): { selector: string; body: string }[] {
  const out: { selector: string; body: string }[] = [];
  for (const m of stripComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selector: m[1].trim().replace(/\s+/g, ' '), body: m[2] });
  }
  return out;
}

const fontSizeOf = (body: string): string | undefined =>
  body.match(/(?:^|[;\s])font-size:\s*([^;}]+)/)?.[1]?.trim();

const pageRules = rules(styleBlock(source));
const ruleFor = (selector: string) => pageRules.find((r) => r.selector === selector);

describe('briefings empty state typography', () => {
  it('parses the page stylesheet', () => {
    // Without this the assertions below pass vacuously the moment the empty
    // state is restructured and the selectors stop matching.
    expect(ruleFor('.empty-state'), 'no .empty-state rule found').toBeDefined();
    expect(ruleFor('.empty-state h1'), 'no .empty-state h1 rule found').toBeDefined();
  });

  it('sizes the empty state from the type scale, not the body default', () => {
    // The container carries the size so the paragraph inherits it rather than
    // falling through to the 1rem `body` default.
    expect(fontSizeOf(ruleFor('.empty-state')!.body)).toBe('var(--text-base)');
    expect(fontSizeOf(ruleFor('.empty-state h1')!.body)).toBe('var(--text-base)');
  });

  it('matches the briefing body it stands in for', () => {
    // The empty state occupies the same slot as a rendered briefing, so the
    // two reading at different scales is the defect however it is spelled.
    expect(fontSizeOf(ruleFor('.body')!.body)).toBe(fontSizeOf(ruleFor('.empty-state')!.body));
  });

  it('writes no font-size in the empty state as a literal', () => {
    for (const rule of pageRules) {
      if (!rule.selector.startsWith('.empty-state')) continue;
      const size = fontSizeOf(rule.body);
      if (size === undefined) continue;
      expect(size, `${rule.selector} sizes text outside the token scale`).toMatch(
        /^var\(--text-[\w-]+\)$/,
      );
    }
  });
});
