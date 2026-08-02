import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { readCascade } from '$lib/styles/cascade';

/**
 * `NoticeBanner` has two type sizes and they belong to the component: the title
 * at `--text-base`, the body at `--text-sm`.
 *
 * Both used to be raw literals (`0.9rem` / `0.85rem`), which is subtler than it
 * looks. A caller cannot read a literal it can only see rendered, so a caller
 * wanting its slot content to sit with the title did the only thing available
 * and wrote its own approximation — the admin standalone notice sized its lead,
 * its labels and its detail lines at 0.9/0.85, and so rendered a whole step
 * above every other banner in the app. It is also the first banner a reader
 * meets on a fresh local install, so the one that looked wrong was the one most
 * likely to be seen.
 *
 * So these tests hold two halves. The component reads tokens rather than
 * literals, and no call site restates a size inside the slot — the point of the
 * banner being one component is that its body is one size everywhere.
 *
 * Same reasoning as contentFrame.test.ts: a caller that hardcodes its own
 * value silently opts out of something it looks like it belongs to, and nothing
 * about the rendered page says so.
 */

const SRC = resolve(process.cwd(), 'src');
const appCss = readCascade();
const componentPath = join(SRC, 'lib/components/ui/NoticeBanner.svelte');
const component = readFileSync(componentPath, 'utf8');

const stripComments = (source: string): string => source.replace(/\/\*[\s\S]*?\*\//g, '');

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

/** Flat `selector { body }` rules. Comments first, since several quote a brace. */
function rules(css: string): Rule[] {
  const out: Rule[] = [];
  for (const m of stripComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selector: m[1].trim().replace(/\s+/g, ' '), body: m[2] });
  }
  return out;
}

/** The `<style>` body of a Svelte file. */
const styleBlock = (source: string): string => {
  const open = source.indexOf('<style');
  if (open === -1) return '';
  return source.slice(source.indexOf('>', open) + 1, source.lastIndexOf('</style>'));
};

const fontSizeOf = (body: string): string | undefined =>
  body.match(/(?:^|[;\s])font-size:\s*([^;}]+)/)?.[1]?.trim();

const remValue = (raw: string | undefined): number => {
  const m = raw?.match(/^([\d.]+)rem$/);
  return m ? Number(m[1]) : NaN;
};

const tokenValue = (name: string): string | undefined => {
  const root = blockAfter(appCss, ':root {') ?? '';
  return stripComments(root)
    .match(new RegExp(`${name}:\\s*([^;]+);`))?.[1]
    ?.trim();
};

/** Every `.svelte` under src/, so a new call site is covered without an edit here. */
function svelteFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...svelteFiles(path));
    else if (entry.name.endsWith('.svelte')) out.push(path);
  }
  return out;
}

interface CallSite {
  /** Path relative to src/, for readable failure messages. */
  name: string;
  /** Classes used on markup inside a `<NoticeBanner>…</NoticeBanner>` slot. */
  slotClasses: string[];
  rules: Rule[];
}

/**
 * Call sites that pass slot content. A self-closing `<NoticeBanner … />` is
 * dropped first: left in, the lazy pair match would run from it to the *next*
 * file's-worth of markup and swallow everything up to a later closing tag,
 * inventing slot classes the banner never contained.
 */
function callSites(): CallSite[] {
  const out: CallSite[] = [];
  for (const path of svelteFiles(SRC)) {
    if (path === componentPath) continue;
    const source = readFileSync(path, 'utf8');
    if (!source.includes('<NoticeBanner')) continue;

    const paired = source.replace(/<NoticeBanner\b[^>]*\/>/g, '');
    const slotClasses = new Set<string>();
    for (const block of paired.matchAll(/<NoticeBanner\b[^>]*>([\s\S]*?)<\/NoticeBanner>/g)) {
      for (const attr of block[1].matchAll(/class="([^"]*)"/g)) {
        for (const cls of attr[1].split(/\s+/).filter(Boolean)) slotClasses.add(cls);
      }
    }
    if (slotClasses.size === 0) continue;

    out.push({
      name: relative(SRC, path),
      slotClasses: [...slotClasses],
      rules: rules(styleBlock(source)),
    });
  }
  return out;
}

const sites = callSites();

describe('NoticeBanner type sizes', () => {
  const componentRules = rules(styleBlock(component));
  const ruleFor = (selector: string) => componentRules.find((r) => r.selector === selector);

  it('parses the component stylesheet', () => {
    // Without this every assertion below passes vacuously the moment the
    // component is restructured and the selectors stop matching.
    expect(ruleFor('.notice-title'), 'no .notice-title rule found').toBeDefined();
    expect(ruleFor('.notice-body'), 'no .notice-body rule found').toBeDefined();
  });

  it('sizes the title and body from type tokens, not literals', () => {
    expect(fontSizeOf(ruleFor('.notice-title')!.body)).toBe('var(--text-base)');
    expect(fontSizeOf(ruleFor('.notice-body')!.body)).toBe('var(--text-sm)');
  });

  it('puts the title above the body on the scale', () => {
    // The banner is a heading over detail; a body at or above the title reads
    // as two paragraphs rather than a titled notice.
    expect(remValue(tokenValue('--text-base'))).toBeGreaterThan(remValue(tokenValue('--text-sm')));
  });

  it('declares no font-size anywhere as a literal', () => {
    for (const rule of componentRules) {
      const size = fontSizeOf(rule.body);
      if (size === undefined) continue;
      expect(size, `${rule.selector} sizes text outside the token scale`).toMatch(
        /^var\(--text-[\w-]+\)$/,
      );
    }
  });
});

describe('NoticeBanner call sites', () => {
  it('finds the slot-bearing call sites', () => {
    // The pair match is the fragile half of this file: an attribute carrying a
    // `>` would end the opening tag early and drop the site silently. If this
    // shrinks to nothing, the suite below is testing air rather than passing.
    expect(sites.length).toBeGreaterThan(0);
  });

  for (const site of sites) {
    it(`${site.name} styles its slot content without restating a size`, () => {
      // A slot rule may set weight, colour, opacity and layout — those are what
      // distinguishes a label from its detail. Size is the component's, and a
      // caller restating it is how the sizes drifted in the first place.
      for (const rule of site.rules) {
        const mentionsSlotClass = site.slotClasses.some((cls) =>
          new RegExp(`\\.${cls.replace(/[.*+?^${}()|[\]\\-]/g, '\\$&')}(?![\\w-])`).test(
            rule.selector,
          ),
        );
        if (!mentionsSlotClass) continue;
        expect(
          fontSizeOf(rule.body),
          `${site.name}: \`${rule.selector}\` sizes text inside the banner slot — ` +
            'the banner body is one size everywhere, set in NoticeBanner.svelte',
        ).toBeUndefined();
      }
    });
  }
});
