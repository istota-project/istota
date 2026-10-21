import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

/**
 * The app half of minot's `typeRoles` test. The heading base, the title
 * classes, `<code>`, `.mono`, `.code-chip`, `.num` and the link classes are
 * minot's and are pinned there; what is left here is the sweep over this
 * app's own files: no component sets a weight on a bare heading without
 * saying why, no file writes a monospace stack except through `--font-mono`,
 * and no page restates one of those classes' typography rather than placing
 * it.
 */

const SRC = join(process.cwd(), 'src');

const stripComments = (css: string): string =>
  css.replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length));

const sourceFiles = (...exts: string[]): string[] => {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) walk(path);
      else if (exts.some((e) => entry.endsWith(e))) out.push(path);
    }
  };
  walk(SRC);
  return out;
};

const rel = (path: string): string => path.slice(SRC.length + 1);

/** A Svelte file's `<style>` block, comments kept: the reason comment is read. */
const styleBlock = (source: string): string => {
  const at = source.indexOf('<style');
  return at === -1 ? '' : source.slice(at);
};

interface Rule {
  selector: string;
  /** The body as written, comments included. */
  raw: string;
  decls: Record<string, string>;
}

/** Innermost `selector { body }` rules, so one inside `@media` is read alone. */
function rules(css: string): Rule[] {
  const out: Rule[] = [];
  // Comments are blanked for the match so a brace inside one cannot split a
  // rule, and the raw body is sliced back out of the original by offset.
  const blank = stripComments(css);
  for (const m of blank.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
    const bodyStart = m.index! + m[0].indexOf('{') + 1;
    const raw = css.slice(bodyStart, bodyStart + m[2].length);
    const decls: Record<string, string> = {};
    for (const part of m[2].split(';')) {
      const colon = part.indexOf(':');
      if (colon === -1) continue;
      decls[part.slice(0, colon).trim()] = part.slice(colon + 1).trim();
    }
    out.push({
      selector: m[1]
        .replace(/<style[^>]*>/, '')
        .trim()
        .replace(/\s+/g, ' '),
      raw,
      decls,
    });
  }
  return out;
}

/**
 * Whether one selector's subject is a heading element with no class of its
 * own: `h2`, `.notes h2`, `:global(.card-body h3)`, `h1:first-child`.
 */
function subjectIsBareHeading(selector: string): boolean {
  const unwrapped = selector.replace(/:global\(([^)]*)\)/g, '$1').trim();
  const compounds = unwrapped.split(/\s*[\s>+~]\s*/).filter(Boolean);
  const subject = compounds[compounds.length - 1] ?? '';
  return /^h[1-4](:[\w-]+(\([^)]*\))?)*$/.test(subject);
}

/** Every rule that sets a weight on a bare heading with no reason given. */
function unexplainedHeadingWeights(css: string): string[] {
  const out: string[] = [];
  for (const rule of rules(css)) {
    if (!('font-weight' in rule.decls)) continue;
    if (!rule.selector.split(',').some((s) => subjectIsBareHeading(s))) continue;
    if (/heading-weight:/.test(rule.raw)) continue;
    out.push(rule.selector);
  }
  return out;
}

/** Every monospace font declaration that does not come through the token. */
function rawMonoStacks(source: string): string[] {
  const out: string[] = [];
  for (const m of stripComments(source).matchAll(/font(?:-family)?\s*:([^;'"`}]*)/g)) {
    const value = m[1];
    if (/\bmonospace\b/.test(value) && !/var\(--font-mono\)/.test(value)) out.push(value.trim());
  }
  return out;
}

describe('heading weights', () => {
  describe('the detector', () => {
    it('finds a weight on a bare heading, however it is reached', () => {
      expect(unexplainedHeadingWeights('h1 { font-weight: 500; }')).toEqual(['h1']);
      expect(unexplainedHeadingWeights('.about h2, .history h2 { font-weight: 500 }')).toEqual([
        '.about h2, .history h2',
      ]);
      expect(unexplainedHeadingWeights(':global(.card-body h3) { font-weight: 600 }')).toEqual([
        ':global(.card-body h3)',
      ]);
    });

    it('lets a classed heading and a placed heading through', () => {
      expect(unexplainedHeadingWeights('h2.name { font-weight: 500 }')).toEqual([]);
      expect(unexplainedHeadingWeights('.x h2 { margin: 0 0 var(--space-2) }')).toEqual([]);
      expect(unexplainedHeadingWeights('.title { font-weight: 600 }')).toEqual([]);
    });

    it('accepts a reason', () => {
      const css = `h1 {
        /* heading-weight: a statement under a glyph, not a page title */
        font-weight: var(--weight-medium);
      }`;
      expect(unexplainedHeadingWeights(css)).toEqual([]);
    });
  });

  it('no component sets a weight on a bare heading without saying why', () => {
    // The base gives every heading its weight. A component that wants
    // another puts a title class on the element, or says why in a
    // `heading-weight:` comment inside the rule.
    const offenders: string[] = [];
    for (const f of sourceFiles('.svelte')) {
      for (const sel of unexplainedHeadingWeights(styleBlock(readFileSync(f, 'utf8')))) {
        offenders.push(`${rel(f)}: ${sel}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe('monospace', () => {
  describe('the detector', () => {
    it('finds a raw stack in a declaration and in an inline style string', () => {
      expect(rawMonoStacks('.x { font-family: ui-monospace, monospace; }')).toEqual([
        'ui-monospace, monospace',
      ]);
      expect(rawMonoStacks(`el.style.cssText = 'font:10px/1.35 Menlo,monospace;'`)).toEqual([
        '10px/1.35 Menlo,monospace',
      ]);
    });

    it('lets the token and prose about monospace through', () => {
      expect(rawMonoStacks('.x { font-family: var(--font-mono); }')).toEqual([]);
      expect(rawMonoStacks('/* font-family: monospace */ .y {}')).toEqual([]);
    });
  });

  it('no file writes a monospace stack except through --font-mono', () => {
    const offenders: string[] = [];
    for (const f of sourceFiles('.svelte', '.css', '.ts')) {
      const file = rel(f);
      if (file.endsWith('.test.ts')) continue;
      for (const stack of rawMonoStacks(readFileSync(f, 'utf8'))) {
        offenders.push(`${file}: ${stack}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe('no page restates the type classes', () => {
  const owned: Record<string, RegExp> = {
    'title-page': /(^|\s)(font-size|font-weight)\s*:/,
    'title-section': /(^|\s)(font-size|font-weight)\s*:/,
    'title-card': /(^|\s)(font-size|font-weight)\s*:/,
    mono: /(^|\s)font-family\s*:/,
    'code-chip': /(^|\s)(font-family|font-size|background|padding|border-radius)\s*:/,
    num: /(^|\s)font-variant-numeric\s*:/,
    'link-action': /(^|\s)(color|background|border|font|cursor)\s*:/,
    'link-quiet': /(^|\s)(color|text-decoration)\s*:/,
    'link-muted': /(^|\s)(color|text-decoration)\s*:/,
  };

  it('no .svelte file declares one of them with its own typography', () => {
    // A page may place one (a margin, a grid span); restating what it looks
    // like gives the role two answers, and the page's wins silently.
    const offenders: string[] = [];
    for (const f of sourceFiles('.svelte')) {
      const css = stripComments(styleBlock(readFileSync(f, 'utf8')));
      for (const [cls, decl] of Object.entries(owned)) {
        const re = new RegExp(`^[ \\t]*\\.${cls}\\s*\\{([^}]*)\\}`, 'gm');
        for (const m of css.matchAll(re)) {
          if (decl.test(m[1])) offenders.push(`${rel(f)}: .${cls}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
