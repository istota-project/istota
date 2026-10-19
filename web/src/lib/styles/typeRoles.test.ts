import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { readLayer } from './cascade';

/**
 * Headings, code, numbers and links: the type roles beside the supporting-text
 * ones in captions.test.ts.
 *
 * Before these existed a heading with no weight fell back to the browser's
 * bold 700, so the same page carried 700, 600 and 500 titles side by side;
 * nine page-title styles and eight card-title variants had grown from that.
 * Monospace was spelled out as five different stacks after `--font-mono` had
 * been added to end exactly that, and the small raised code chip was written
 * seven times.
 *
 * So these tests pin the classes in primitives.css, fail a component that sets
 * a weight on a bare heading without saying why, fail any monospace stack that
 * does not come through the token, and fail a page that restates one of the
 * new classes' typography rather than only placing it.
 */

const primitives = readLayer('primitives');
const SRC = join(process.cwd(), 'src');

const stripComments = (css: string): string =>
  css.replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length));

/** The body of a top-level rule in primitives.css, by its exact selector. */
const ruleBody = (selector: string): string | undefined => {
  const css = stripComments(primitives);
  const at = css.indexOf(`\n${selector} {`);
  if (at === -1) return undefined;
  return css.slice(at, css.indexOf('}', at));
};

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
export function subjectIsBareHeading(selector: string): boolean {
  const unwrapped = selector.replace(/:global\(([^)]*)\)/g, '$1').trim();
  const compounds = unwrapped.split(/\s*[\s>+~]\s*/).filter(Boolean);
  const subject = compounds[compounds.length - 1] ?? '';
  return /^h[1-4](:[\w-]+(\([^)]*\))?)*$/.test(subject);
}

/** Every rule that sets a weight on a bare heading with no reason given. */
export function unexplainedHeadingWeights(css: string): string[] {
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
export function rawMonoStacks(source: string): string[] {
  const out: string[] = [];
  for (const m of stripComments(source).matchAll(/font(?:-family)?\s*:([^;'"`}]*)/g)) {
    const value = m[1];
    if (/\bmonospace\b/.test(value) && !/var\(--font-mono\)/.test(value)) out.push(value.trim());
  }
  return out;
}

describe('the heading base', () => {
  it('starts every heading at semibold with no margin, weightless', () => {
    const at = primitives.indexOf(':where(h1, h2, h3, h4)');
    expect(at, 'no heading base in primitives.css').toBeGreaterThan(-1);
    const body = primitives.slice(at, primitives.indexOf('}', at));
    // Without it a heading with no weight renders at the browser's 700,
    // which is how settings cards (700), modals (600) and health pages (500)
    // came to disagree on the same role.
    expect(body).toContain('font-weight: var(--weight-semibold)');
    expect(body).toMatch(/margin: 0;/);
  });

  it('leaves rendered content its own heading rhythm', () => {
    // A feed entry or a workspace document brings its own headings, which
    // collapse onto their paragraphs with the margin taken away.
    expect(primitives).toMatch(/:where\(h1, h2, h3, h4\):not\(:where\(\.prose, \.prose \*\)\)/);
  });
});

describe('the title classes', () => {
  const cases: [string, string, string][] = [
    // Q10: the page title is the large step at semibold.
    ['.title-page', 'var(--text-lg)', 'var(--weight-semibold)'],
    ['.title-section', 'var(--text-base)', 'var(--weight-semibold)'],
    // Q6: a card title sits a weight under the section title above it.
    ['.title-card', 'var(--text-sm)', 'var(--weight-medium)'],
  ];
  for (const [selector, size, weight] of cases) {
    it(`${selector} is ${size} at ${weight}, and typography only`, () => {
      const body = ruleBody(selector);
      expect(body, selector).toBeDefined();
      expect(body).toContain(`font-size: ${size}`);
      expect(body).toContain(`font-weight: ${weight}`);
      expect(body).not.toMatch(/^\s*(margin|padding|display|color)/m);
    });
  }

  it('steps down from page to section to card', () => {
    const sizes = ['--text-lg', '--text-base', '--text-sm'].map((t) =>
      parseFloat(new RegExp(`${t}:\\s*([\\d.]+)rem`).exec(readLayer('tokens'))![1]),
    );
    expect(sizes[0]).toBeGreaterThan(sizes[1]);
    expect(sizes[1]).toBeGreaterThan(sizes[2]);
  });
});

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

describe('code and monospace', () => {
  it('gives <code> the mono face a step under its text', () => {
    const body = ruleBody('code');
    expect(body).toBeDefined();
    expect(body).toContain('font-family: var(--font-mono)');
    expect(body).toContain('font-size: 0.9em');
  });

  it('.mono is the face alone', () => {
    const body = ruleBody('.mono');
    expect(body).toBeDefined();
    expect(body).toContain('font-family: var(--font-mono)');
    expect(body).not.toMatch(/font-size|color|background/);
  });

  it('.code-chip is the raised mono chip', () => {
    const body = ruleBody('.code-chip');
    expect(body).toBeDefined();
    for (const decl of [
      'font-family: var(--font-mono)',
      'font-size: var(--text-xs)',
      'background: var(--surface-raised)',
      'padding: 0 var(--space-1)',
      'border-radius: var(--radius-sm)',
    ]) {
      expect(body).toContain(decl);
    }
  });

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
      if (file === 'lib/styles/tokens.css' || file.endsWith('.test.ts')) continue;
      for (const stack of rawMonoStacks(readFileSync(f, 'utf8'))) {
        offenders.push(`${file}: ${stack}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe('.num', () => {
  it('lines digits up and does nothing else', () => {
    const body = ruleBody('.num');
    expect(body).toBeDefined();
    expect(body).toContain('font-variant-numeric: tabular-nums');
    // The table family adds alignment on its own `.table .num`; here a count
    // in a pill must not be pushed to one side.
    expect(body).not.toMatch(/text-align|white-space/);
  });
});

describe('the link classes', () => {
  it('.link-action is a button drawn as a content link', () => {
    const body = ruleBody('.link-action');
    expect(body).toBeDefined();
    for (const decl of [
      'background: none',
      'border: none',
      'padding: 0',
      'font: inherit',
      // `font: inherit` resets the leading too, and the page's must win.
      'line-height: inherit',
      'color: var(--link)',
      'cursor: pointer',
    ]) {
      expect(body).toContain(decl);
    }
  });

  for (const [cls, colour] of [
    ['.link-action', 'var(--link)'],
    ['.link-quiet', 'var(--text-primary)'],
    ['.link-muted', 'var(--text-muted)'],
  ]) {
    it(`${cls} keeps its colour on hover and shows the underline`, () => {
      // The bare `a:hover` brightens a link to --accent-hover; these keep
      // their colour and let the underline mark the state.
      const hover = ruleBody(`${cls}:hover`);
      expect(hover, `${cls}:hover`).toBeDefined();
      expect(hover).toContain(`color: ${colour}`);
      expect(hover).toContain('text-decoration: underline');
    });
  }

  it('.link-quiet and .link-muted carry no underline at rest', () => {
    expect(ruleBody('.link-quiet')).toContain('text-decoration: none');
    expect(ruleBody('.link-muted')).toContain('text-decoration: none');
  });
});

describe('no page restates the new classes', () => {
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
