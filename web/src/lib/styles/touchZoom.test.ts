import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { readCascade } from './cascade';

/**
 * iOS zooms the page whenever a focused text control computes under 16px, and
 * does not cleanly zoom back out (ISSUE-219). The floor that stops it lives in
 * `app.css` as a coarse-pointer redefinition of the type tokens *on the control
 * itself*, so it holds regardless of which rule wins a component's font-size.
 *
 * These tests hold the two halves of that: the floor covers every type token
 * that renders under the line, and no stylesheet sizes a control in a way that
 * reads no token and so slips underneath it.
 */

const SRC = resolve(process.cwd(), 'src');
const css = readCascade();

/**
 * The `small` text-scale preference sets no root font-size, so with the browser
 * at its own 16px default this is the smallest root the app renders at — and
 * therefore where a rem token is at its smallest. A user who has *lowered* their
 * browser base font is outside this: the root is a percentage of it (app.css
 * scales 110%/120%), so everything shrinks with it and a token above the line
 * here could fall under it there.
 */
const ROOT_PX = 16;
const ZOOM_FLOOR_PX = 16;

/** Body of the first block whose header starts at `needle`, braces balanced. */
function blockAfter(source: string, needle: string): string | null {
  const at = source.indexOf(needle);
  if (at === -1) return null;
  const open = source.indexOf('{', at);
  if (open === -1) return null;
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, i);
    }
  }
  return null;
}

interface Rule {
  selector: string;
  body: string;
}

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** Flat `selector { body }` rules of a block body. Comments are stripped first:
 *  five in this tree quote a brace, which desynchronizes the walk. */
function rules(body: string): Rule[] {
  const out: Rule[] = [];
  for (const m of stripComments(body).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selector: m[1].trim().replace(/\s+/g, ' '), body: m[2] });
  }
  return out;
}

/** `--name: value` pairs declared anywhere in a block body. */
function customProps(body: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of body.matchAll(/(--[\w-]+)\s*:\s*([^;}]+);/g)) {
    out.set(m[1], m[2].trim());
  }
  return out;
}

/** px value of a bare `Nrem`; null for anything this cannot evaluate. */
function remToPx(value: string): number | null {
  const m = /^([\d.]+)rem$/.exec(value.trim());
  return m ? parseFloat(m[1]) * ROOT_PX : null;
}

const rootBody = blockAfter(css, ':root {') ?? '';
const coarseBody = blockAfter(css, '@media (pointer: coarse)') ?? '';
const coarseRules = rules(coarseBody);

/** The rule carrying the token floors, the one carrying the fallback, and the
 *  paired-control floor. Located by selector, not by position — the block is a
 *  natural home for further touch rules. */
const tokenRule = coarseRules.find(
  (r) => r.selector.includes('textarea') && customProps(r.body).has('--text-sm'),
);
const fallbackRule = coarseRules.find((r) => /(?:^|[;\s])font-size:/.test(r.body));
const selectTriggerRule = coarseRules.find((r) => r.selector.includes('ui-select-trigger'));

/** `--text-*` is two families: the type scale and the text *colours*
 *  (`--text-primary`, `--text-dim`, …). Only the scale is in scope, and it is
 *  identified by not being a colour rather than by a name list, so a token
 *  added to the scale is picked up without editing this file. */
const isColor = (value: string) => /^(#|rgba?\(|hsla?\(|color\()/.test(value.trim());
const typeTokens = [...customProps(rootBody)].filter(
  ([name, value]) => name.startsWith('--text-') && !isColor(value),
);

/** Type tokens that render below the zoom line and so must be floored. */
const belowLine = typeTokens
  .map(([name, value]) => [name, remToPx(value), value] as const)
  .filter(([, px]) => px !== null && px < ZOOM_FLOOR_PX);

describe('app.css touch-zoom floor', () => {
  it('parses the block and both of its rules', () => {
    expect(rootBody).not.toBe('');
    expect(coarseBody).not.toBe('');
    expect(tokenRule, 'no rule in the coarse block declares --text-sm').toBeDefined();
    expect(fallbackRule, 'no rule in the coarse block declares font-size').toBeDefined();
    // Without this the floor assertions pass vacuously if the parse stops
    // matching: the app does have type tokens under 16px.
    expect(belowLine.length).toBeGreaterThan(0);
  });

  it('evaluates every type token rather than skipping the ones it cannot read', () => {
    // remToPx returns null for `13px`, `calc(...)`, `clamp(...)`. Left to the
    // filter above, such a token would drop out of `belowLine` and so escape
    // the floor requirement instead of failing here.
    const unreadable = typeTokens.filter(([, value]) => remToPx(value) === null);
    expect(unreadable.map(([n, v]) => `${n}: ${v}`)).toEqual([]);
  });

  it.each([
    ['token floor', () => tokenRule],
    ['fallback', () => fallbackRule],
  ])('applies the %s to every control iOS zooms for', (_label, get) => {
    const selector = get()?.selector ?? '';
    for (const el of ['input', 'select', 'textarea']) {
      expect(new RegExp(`(^|[\\s,(])${el}(?![\\w-])`).test(selector)).toBe(true);
    }
    // Checkboxes and radios carry no text, and some platforms size the box
    // from the font.
    for (const type of ['checkbox', 'radio']) {
      expect(selector).toContain(`:not([type='${type}'])`);
    }
  });

  it.each(belowLine)('floors %s on a coarse pointer', (name, _px, value) => {
    // Written against the same base value the token carries at :root — the
    // floor is a max(), not a replacement, so a larger text-scale preference is
    // never scaled back down. Asserting the pair here is what stops the two
    // copies of the value drifting apart.
    const declared = customProps(tokenRule?.body ?? '').get(name);
    expect(declared, `${name} has no coarse-pointer floor`).toBeDefined();
    expect(declared?.replace(/\s+/g, '')).toBe(`max(${value},${ZOOM_FLOOR_PX}px)`);
  });

  it('gives a control that names no token at all a floored fallback', () => {
    // The UA font for a control is ~13px, also under the line, so the block
    // needs a fallback as well as the token floors.
    const declared = /font-size:\s*([^;}]+)/.exec(fallbackRule?.body ?? '')?.[1]?.trim();
    const named = /^var\((--text-[\w-]+)\)$/.exec(declared ?? '')?.[1];
    expect(named, `fallback font-size should name a floored token, got ${declared}`).toBeDefined();
    expect(belowLine.map(([n]) => n)).toContain(named);
  });

  it('floors the full-width Select trigger so form fields stay in step', () => {
    // A <button>, so it never zoomed — but Select.svelte matches its height to
    // the inputs it sits beside in forms, and lifting only the inputs would put
    // the two back out of step. Same base value, so they land together.
    const declared = customProps(selectTriggerRule?.body ?? '').get('--text-sm');
    const control = customProps(tokenRule?.body ?? '').get('--text-sm');
    expect(selectTriggerRule?.selector).toContain('--full');
    expect(declared, 'the full-width Select trigger has no floor').toBeDefined();
    expect(declared).toBe(control);
  });

  it('keeps the fallback weightless so a component can still ask for more', () => {
    // The :not() chain outscores almost every component rule, so an unwrapped
    // fallback would clamp a control that legitimately wants a larger size
    // rather than merely catching one that states none.
    const selector = fallbackRule?.selector ?? '';
    expect(selector.startsWith(':where(')).toBe(true);
    expect(selector.endsWith(')')).toBe(true);
  });
});

function styleFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) styleFiles(path, out);
    else if (/\.(svelte|css)$/.test(entry.name)) out.push(path);
  }
  return out;
}

/** Every `<style>` body in a component, or the whole file for a stylesheet. */
function styleBlocks(file: string, source: string): string[] {
  if (file.endsWith('.css')) return [source];
  return [...source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]);
}

/**
 * A control whose size is inherited or written as a literal reads no token, so
 * the floor above cannot reach it. Each exemption states why it is one.
 *
 * Keyed on file *and* selector rather than file alone: a file-wide entry would
 * blanket a control added to that component later, which is the failure
 * `web/AGENTS.md` records for `design-lint-allow-file`.
 */
const SIZED_ELSEWHERE = new Map([
  [
    '/lib/components/chat/Composer.svelte: textarea',
    'the pill around the textarea owns the font-size and every internal metric ' +
      'is em-relative to it, so the composer carries its own focus-time viewport ' +
      'guard instead',
  ],
]);

describe('stylesheets do not slip under the floor', () => {
  // The `(` in the leading class is load-bearing: `:global(input)` is this
  // codebase's dominant idiom for styling a control from a wrapper — it is how
  // `ui/Field.svelte` sizes most of the app's inputs — and it also covers
  // `:is()`/`:where()`. Only element-selected rules are checked: a class name
  // says nothing about which element it lands on, so a broader scan would be
  // guesswork. That makes this a net rather than a proof.
  const CONTROL_SELECTOR = /(^|[\s,>+~(])(input|textarea|select)(?![\w-])/;

  it('sizes every element-selected control through a floored token', () => {
    const offenders: string[] = [];
    for (const file of styleFiles(SRC)) {
      const rel = file.slice(SRC.length);
      const source = readFileSync(file, 'utf8');
      for (const block of styleBlocks(file, source)) {
        for (const rule of rules(block)) {
          if (!CONTROL_SELECTOR.test(rule.selector)) continue;
          if (SIZED_ELSEWHERE.has(`${rel}: ${rule.selector}`)) continue;
          const size = /(?:^|[;\s])font-size:\s*([^;}]+)/.exec(rule.body)?.[1]?.trim();
          const shorthand = /(?:^|[;\s])font:\s*([^;}]+)/.exec(rule.body)?.[1]?.trim();
          // The shorthand resets font-size, so a rule carrying only `font:`
          // takes its size from whatever it names (`inherit`, in practice).
          const effective = size ?? (shorthand ? `font: ${shorthand}` : undefined);
          if (!effective) continue;
          if (effective.startsWith('var(--text-')) continue;
          offenders.push(`${rel}: ${rule.selector} → ${effective}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it('catches a control sized past the floor however the rule is written', () => {
    // A guard on the guard: the scan is a regex over stylesheet text, and its
    // failure mode is matching nothing at all. These are the four shapes the
    // tree actually uses.
    for (const selector of [
      '.field input',
      ".field :global(input:not([type='checkbox'])), .field :global(textarea)",
      'input, select',
      '.value-row select.unit-select',
    ]) {
      expect(CONTROL_SELECTOR.test(selector), selector).toBe(true);
    }
    expect(CONTROL_SELECTOR.test('.nav-select, .select-trigger')).toBe(false);
  });

  it('keeps every exemption pointed at a rule that still exists', () => {
    // An exemption outliving its rule is an exemption nobody is reading, and
    // the next control matching that selector inherits it silently.
    const live = new Set<string>();
    for (const file of styleFiles(SRC)) {
      const rel = file.slice(SRC.length);
      const source = readFileSync(file, 'utf8');
      for (const block of styleBlocks(file, source)) {
        for (const rule of rules(block)) live.add(`${rel}: ${rule.selector}`);
      }
    }
    for (const key of SIZED_ELSEWHERE.keys()) expect(live).toContain(key);
  });
});
