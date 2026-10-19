import { describe, it, expect, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { compile } from 'svelte/compiler';
import { cleanup, render } from '@testing-library/svelte';
import { blockAfter, readLayer, rules, styleBlocks, styleFiles } from '$lib/styles/cascade';
import FieldCascadeHarness from './FieldCascade.harness.svelte';

/**
 * `Field` styles the raw controls written inside it through descendant rules.
 * Those rules used to score (0,3,1), above `Input`'s `.ui-input.mono` and
 * `.ui-input.invalid` at (0,3,0), so inside a field a monospace Input rendered
 * sans and an invalid one showed no red border, and every bare TextArea was
 * forced into the mono face. jsdom does not run Svelte's scoped CSS through a
 * cascade, so these tests read the compiled stylesheet instead and check the
 * selectors that decide it.
 */

afterEach(cleanup);

const uiDir = join(process.cwd(), 'src/lib/components/ui');

function compiledCss(file: string): string {
  const filename = join(uiDir, file);
  const css = compile(readFileSync(filename, 'utf8'), {
    filename,
    generate: 'client',
    css: 'external',
  }).css!.code;
  return css;
}

const CONTROL = /(^|[\s,>+~(])(input|textarea|select)(?![\w-])/;

/** A selector list split at its top-level commas, not those inside `:where()`. */
function selectorList(selector: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < selector.length; i++) {
    const c = selector[i];
    if (c === '(') depth += 1;
    else if (c === ')') depth -= 1;
    else if (c === ',' && depth === 0) {
      out.push(selector.slice(start, i).trim());
      start = i + 1;
    }
  }
  out.push(selector.slice(start).trim());
  return out;
}

/** Rules of Field's compiled sheet whose selector reaches a text control. */
const fieldControlRules = rules(compiledCss('Field.svelte')).filter(
  (r) => CONTROL.test(r.selector) && !/\[type='checkbox'\]\s*$/.test(r.selector),
);

/** A compiled selector with Svelte's scope classes removed, for `matches()`. */
const unscoped = (selector: string) =>
  selector.replace(/:where\(\.svelte-[\w-]+\)/g, '').replace(/\.svelte-[\w-]+/g, '');

describe("Field's descendant control rules", () => {
  it('finds the rules it is about', () => {
    // A parse that stops matching would pass every test below vacuously.
    expect(fieldControlRules.length).toBeGreaterThanOrEqual(4);
  });

  it('wraps every descendant control selector in :where()', () => {
    // Scoped `.field` plus a weightless :where() is (0,2,0): any class on the
    // control itself, Input's `.mono` and `.invalid` included, now outranks it.
    for (const rule of fieldControlRules) {
      for (const part of selectorList(rule.selector)) {
        expect(part, rule.selector).toMatch(/^\.field[\w-]*\.svelte-[\w-]+ :where\(/);
      }
    }
  });

  it('keeps the appearance off the Input and TextArea primitives', () => {
    const appearance = fieldControlRules.filter((r) => /(^|[\s;])border:/.test(r.body));
    expect(appearance.length).toBe(1);
    expect(appearance[0].selector).toContain('.ui-input');
    expect(appearance[0].selector).toContain('textarea:not(.ui-textarea)');
  });

  it('no longer forces the mono face on a textarea', () => {
    for (const rule of fieldControlRules) {
      expect(rule.body, rule.selector).not.toMatch(/font-family:\s*var\(--font-mono\)/);
    }
  });

  it('matches a raw control and leaves the primitives alone, in the DOM', () => {
    render(FieldCascadeHarness);
    const appearance = fieldControlRules.find((r) => /(^|[\s;])border:/.test(r.body))!;
    const selector = unscoped(appearance.selector);
    const raw = document.querySelector<HTMLElement>('[aria-label="Raw"]')!;
    const input = document.querySelector<HTMLElement>('[aria-label="Primitive"]')!;
    const area = document.querySelector<HTMLElement>('[aria-label="Area"]')!;
    expect(raw.matches(selector)).toBe(true);
    expect(input.matches(selector)).toBe(false);
    expect(area.matches(selector)).toBe(false);
    // What the props asked for is on the element for Input's own rules to style.
    expect(input).toHaveClass('ui-input', 'mono', 'invalid');
    expect(input).toHaveAttribute('aria-invalid', 'true');
  });

  it("still caps a primitive's width inside a field", () => {
    // The geometry half deliberately reaches the primitives, so an Input in a
    // form keeps the 24rem cap it always had.
    render(FieldCascadeHarness);
    const geometry = fieldControlRules.find(
      (r) => /max-width:\s*24rem/.test(r.body) && !/(^|[\s;])border:/.test(r.body),
    )!;
    const input = document.querySelector<HTMLElement>('[aria-label="Primitive"]')!;
    expect(input.matches(unscoped(geometry.selector))).toBe(true);
  });
});

describe('Input and TextArea draw their own props', () => {
  it.each([
    ['Input.svelte', 'ui-input'],
    ['TextArea.svelte', 'ui-textarea'],
  ])('%s styles mono and invalid on its own class', (file, cls) => {
    const css = compiledCss(file).replace(/\s+/g, ' ');
    expect(css).toMatch(
      new RegExp(`\\.${cls}\\.mono\\.svelte-[\\w-]+ \\{ font-family: var\\(--font-mono\\);`),
    );
    expect(css).toMatch(
      new RegExp(
        `\\.${cls}\\.invalid\\.svelte-[\\w-]+ \\{ border-color: var\\(--status-danger-fg\\);`,
      ),
    );
  });
});

describe('the text-field focus state', () => {
  const primitives = readLayer('primitives');
  const focusRule = rules(primitives).find((r) =>
    /border-color:\s*var\(--focus-field-border\)/.test(r.body),
  );
  const selectors = selectorList(focusRule?.selector ?? '');

  it('is one rule in primitives.css', () => {
    expect(focusRule).toBeDefined();
  });

  it.each([
    'input.ui-input:focus-visible',
    'textarea.ui-textarea:focus-visible',
    'button.ui-select-trigger--full:focus-visible',
    '.field select:focus-visible',
  ])('covers %s', (selector) => {
    expect(selectors).toContain(selector);
  });

  it("covers the raw input and textarea a Field styles, but not Field's checkbox", () => {
    const rawInput = selectors.find((s) => s.startsWith('.field input'));
    const rawArea = selectors.find((s) => s.startsWith('.field textarea'));
    expect(rawInput).toContain("[type='checkbox']");
    expect(rawInput).toContain('.ui-input');
    expect(rawArea).toContain('.ui-textarea');
  });

  it('outranks the primitives at rest and stays under .invalid', () => {
    // `input.ui-input:focus-visible` is (0,2,1): above `.ui-input.svelte-x`
    // (0,2,0), below `.ui-input.invalid.svelte-x` (0,3,0). A bare
    // `.ui-input:focus-visible` would tie with the resting rule and lose to
    // whichever stylesheet the bundler put later.
    expect(selectors[0]).toMatch(/^input\.ui-input:focus-visible$/);
  });

  it('defines --focus-field-border in both themes', () => {
    const tokens = readLayer('tokens');
    const dark = blockAfter(tokens, ':root {') ?? '';
    const light = blockAfter(tokens, ":root[data-theme='light'] {") ?? '';
    expect(dark).toMatch(/--focus-field-border:\s*var\(--accent-blue\)/);
    expect(light).toMatch(/--focus-field-border:\s*var\(--accent-blue\)/);
  });

  it('is the only colour a focus rule gives a border, outside the composer', () => {
    // A bare control that draws its own edge (the taxes adorned input, the
    // paste box, the feeds title cell) keeps a local focus rule, because the
    // shared one cannot reach it. What it may not do is pick its own colour,
    // which is how four focus treatments came to exist.
    const COMPOSER = '/lib/components/chat/Composer.svelte: .composer-row:focus-within';
    const src = join(process.cwd(), 'src');
    const offenders: string[] = [];
    for (const file of styleFiles(src)) {
      const rel = file.slice(src.length);
      for (const block of styleBlocks(file, readFileSync(file, 'utf8'))) {
        for (const rule of rules(block)) {
          // Focus-only rules: a hover-and-focus pair on a link or a button is
          // a hover look reused for the keyboard, not a text field's focus.
          const focusOnly = selectorList(rule.selector).every((s) => /:focus/.test(s));
          if (!focusOnly) continue;
          const color = /(?:^|[;\s])border-color:\s*([^;}]+)/.exec(rule.body)?.[1]?.trim();
          if (!color || color === 'var(--focus-field-border)') continue;
          if (`${rel}: ${rule.selector}` === COMPOSER) continue;
          offenders.push(`${rel}: ${rule.selector} → ${color}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
