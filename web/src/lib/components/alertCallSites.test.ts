import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { rules } from '$lib/styles/cascade';

/**
 * A bordered `Alert`'s type sizes belong to the component (`--text-base` for
 * the title, `--text-sm` for the body), and minot's own `Alert.typography`
 * test holds the component half. This is the call-site half: no page here
 * restates a size inside the slot. The admin standalone notice once sized its
 * lead, labels and detail lines at 0.9/0.85rem and so rendered a step above
 * every other banner, and it is the first banner a reader meets on a fresh
 * local install.
 */

const SRC = resolve(process.cwd(), 'src');

/** The `<style>` body of a Svelte file. */
const styleBlock = (source: string): string => {
  const open = source.indexOf('<style');
  if (open === -1) return '';
  return source.slice(source.indexOf('>', open) + 1, source.lastIndexOf('</style>'));
};

const fontSizeOf = (body: string): string | undefined =>
  body.match(/(?:^|[;\s])font-size:\s*([^;}]+)/)?.[1]?.trim();

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
  /** Classes used on markup inside an `<Alert>…</Alert>` slot. */
  slotClasses: string[];
  rules: ReturnType<typeof rules>;
}

/**
 * Call sites that pass slot content. A self-closing `<Alert … />` is dropped
 * first: left in, the lazy pair match would run from it to the next closing
 * tag and invent slot classes the banner never contained.
 */
function callSites(): CallSite[] {
  const out: CallSite[] = [];
  for (const path of svelteFiles(SRC)) {
    const source = readFileSync(path, 'utf8');
    if (!source.includes('<Alert')) continue;
    const slotClasses = new Set<string>();
    const paired = source.replace(/<Alert\b[^>]*\/>/g, '');
    for (const block of paired.matchAll(/<Alert\b[^>]*>([\s\S]*?)<\/Alert>/g)) {
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

describe('Alert call sites', () => {
  it('finds the slot-bearing call sites', () => {
    // The pair match is the fragile half of this file: an attribute carrying a
    // `>` would end the opening tag early and drop the site silently. If this
    // shrinks to nothing, the suite below is testing air rather than passing.
    expect(sites.length).toBeGreaterThan(0);
  });

  for (const site of sites) {
    it(`${site.name} styles its slot content without restating a size`, () => {
      // A slot rule may set weight, colour, opacity and layout — those are what
      // distinguishes a label from its detail. Size is the component's.
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
            'the alert body is one size everywhere, set by Alert in @minot/ui',
        ).toBeUndefined();
      }
    });
  }
});
