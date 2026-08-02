import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { readCascade } from './cascade';

/**
 * Four more of the small shared blocks, on the same terms as `.micro-label`,
 * `.empty` and `.caption` before them: the rule lives in one place, and the
 * spacing that varied per call site stays at the call site.
 *
 * What each was before:
 *
 *   .form-error    five money forms, byte-identical bar one declaration —
 *                  whether the space belonged above it or below it
 *   .form-actions  five health pages, all flush right
 *   .kv            four files, identical but for the column gap
 *   .card-head     five files in two clusters, agreeing on the row and
 *                  disagreeing on how it aligns a title that wraps
 *
 * The interesting case is the last one. `.card-head` is kept to the part the
 * two clusters agree on — a row with a title at one end and its actions at
 * the other — because `align-items` genuinely differs: `flex-start` where a
 * long record name wraps to a second line and a centred row would float its
 * actions against the middle of it, `center` where the title is always one
 * line. Folding a disagreement in would be picking a winner silently, which
 * is what `.meta` did before the captions pass unpicked it.
 */

const cascade = readCascade();

/** The body of a top-level rule, by selector. */
const ruleBody = (css: string, selector: string): string | undefined => {
  const at = css.indexOf(`\n${selector} {`);
  if (at === -1) return undefined;
  return css.slice(at, css.indexOf('}', at));
};

const svelteFiles = (): string[] => {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) walk(path);
      else if (entry.endsWith('.svelte')) out.push(path);
    }
  };
  walk(join(process.cwd(), 'src'));
  return out;
};

const styleBlock = (source: string): string => {
  const at = source.indexOf('<style');
  if (at === -1) return '';
  return source.slice(at).replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length));
};

/** The bodies of every bare `.<cls> { … }` rule in a stylesheet. */
const bareRuleBodies = (css: string, cls: string): string[] => {
  const out: string[] = [];
  const re = new RegExp(`^[ \\t]*\\.${cls}\\s*\\{([^}]*)\\}`, 'gm');
  for (const m of css.matchAll(re)) out.push(m[1]);
  return out;
};

describe('.form-error', () => {
  const body = ruleBody(cascade, '.form-error');

  it('is defined once, small and in the danger colour', () => {
    expect(body).toBeDefined();
    expect(body).toContain('color: var(--status-danger-fg)');
    expect(body).toContain('font-size: var(--text-xs)');
  });

  it('carries no margin of its own', () => {
    // The one declaration the five copies disagreed on: three put the space
    // above it, two below. That is the call site's decision — it depends on
    // what the error sits between, not on what an error looks like.
    expect(body).not.toMatch(/^\s*margin/m);
  });
});

describe('.form-actions', () => {
  const body = ruleBody(cascade, '.form-actions');

  it('is a flush-right row with a gap', () => {
    expect(body).toBeDefined();
    expect(body).toContain('display: flex');
    expect(body).toContain('justify-content: flex-end');
    // Four of the five held a single button, so their missing gap was
    // invisible rather than deliberate — the fifth, with two, had to add it.
    expect(body).toContain('gap: var(--space-2)');
  });
});

describe('.kv', () => {
  const body = ruleBody(cascade, '.kv');

  it('is a two-column grid: label at content width, value filling the rest', () => {
    expect(body).toBeDefined();
    expect(body).toContain('grid-template-columns: max-content 1fr');
  });

  it('takes its column gap from a hook, so a wide table can open it up', () => {
    // The only thing the four copies disagreed on: /admin's rows are short
    // key/value pairs that read better further apart. Same shape as
    // cards.css's --card-min / --card-gap.
    expect(body).toMatch(/--kv-gap/);
  });
});

describe('.card-head', () => {
  const body = ruleBody(cascade, '.card-head');

  it('is a row with a title at one end and its actions at the other', () => {
    expect(body).toBeDefined();
    expect(body).toContain('display: flex');
    expect(body).toContain('justify-content: space-between');
  });

  it('does not decide how the row aligns', () => {
    // The clusters disagree, and both are right — see the note above.
    expect(body).not.toMatch(/align-items/);
  });
});

describe('no page re-declares a shared block', () => {
  const files = svelteFiles();

  for (const cls of ['form-error', 'form-actions', 'kv', 'card-head'] as const) {
    it(`no .svelte file restates .${cls}`, () => {
      // As with .caption: a call site MAY declare the class to place or align
      // it. What it may not do is restate the block itself, because then two
      // files answer the same question and the page's answer wins silently.
      const restated = /(^|\s)(display|grid-template-columns|justify-content|color|font-size)\s*:/;
      const offenders: string[] = [];
      for (const f of files) {
        for (const body of bareRuleBodies(styleBlock(readFileSync(f, 'utf8')), cls)) {
          if (restated.test(body)) offenders.push(f.slice(f.indexOf('src/')));
        }
      }
      expect(offenders, `.${cls} belongs in primitives.css`).toEqual([]);
    });
  }
});
