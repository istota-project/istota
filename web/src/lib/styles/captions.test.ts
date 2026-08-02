import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { readCascade } from './cascade';

/**
 * The two supporting-text roles: `.caption` and `.muted`.
 *
 * Both existed everywhere and were named nowhere. "Small dim text" was spelled
 * `.hint` on four pages, `.meta` on five more and `.small` on two, and
 * "de-emphasized at the current size" was `.muted` in nine files — each one
 * written out again because there was nothing to reach for. `settings.css` had
 * the roles already, but scoped under `.settings`, so a page outside a settings
 * route could only get them by re-declaring them.
 *
 * That is `.micro-label`'s story one role over, and it had already produced the
 * drift those consolidations exist to catch: `/admin` sat its `.muted` on
 * `--text-dim` at `--text-sm`, a step off the eight files spelling the same
 * class `--text-muted` at the inherited size. Nothing about either page says
 * which one is the app's answer.
 *
 * So these tests do not check that the CSS parses. They check that each role
 * stays in ONE place, that it stays typography-only — spacing is what forked
 * the copies, and it belongs at the call site — and that the two roles keep the
 * distinct tokens that make them two roles rather than one.
 *
 * Same reasoning as contentFrame.test.ts and controlTier.test.ts.
 */

const appCss = readCascade();

/** The body of a top-level rule, by selector. */
const ruleBody = (css: string, selector: string): string | undefined => {
  const at = css.indexOf(`\n${selector} {`);
  if (at === -1) return undefined;
  return css.slice(at, css.indexOf('}', at));
};

/** Every .svelte file in the tree. */
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

/**
 * A file's `<style>` block with comments blanked out. Several of these files
 * explain in prose why a rule moved to app.css, and a naive scan finds the
 * class name in the sentence saying it is no longer here. Blanked rather than
 * deleted so offsets still line up.
 */
const styleBlock = (source: string): string => {
  const at = source.indexOf('<style');
  if (at === -1) return '';
  return source.slice(at).replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length));
};

/** Declares `selector` as a rule of its own (not as part of a compound). */
const declaresRule = (css: string, cls: string): boolean =>
  new RegExp(`^[ \\t]*\\.${cls}\\s*\\{`, 'm').test(css);

/** The bodies of every bare `.<cls> { … }` rule in a stylesheet. */
const bareRuleBodies = (css: string, cls: string): string[] => {
  const out: string[] = [];
  const re = new RegExp(`^[ \\t]*\\.${cls}\\s*\\{([^}]*)\\}`, 'gm');
  for (const m of css.matchAll(re)) out.push(m[1]);
  return out;
};

/**
 * `.meta` is the one name the migration deliberately left overloaded. Three
 * files use it for something that is not supporting text at all — the chat
 * message's author/timestamp row, the bloodwork marker's stat row, and the
 * encounter page's card surface — and folding a layout rule into a typography
 * global would say they were the same thing. They keep the name locally;
 * a Svelte-scoped rule outranks a global, so nothing collides.
 */
const META_IS_LAYOUT = [
  'lib/components/chat/Message.svelte',
  'routes/health/bloodwork/marker/+page.svelte',
  'routes/health/history/encounter/+page.svelte',
  'routes/briefings/+page.svelte',
];

describe('.caption', () => {
  const body = ruleBody(appCss, '.caption');

  it('is defined once, on the dim token at the small step', () => {
    expect(body).toBeDefined();
    // --text-dim (#666) rather than --text-muted (#888): these are two
    // distinct steps, and the size going down with the colour is what makes
    // this a caption rather than merely de-emphasized text.
    expect(body).toContain('color: var(--text-dim)');
    expect(body).toContain('font-size: var(--text-xs)');
  });

  it('is typography only', () => {
    // The division .micro-label draws, for the reason it draws it: a hint
    // under a field and a timestamp under a title want different space around
    // them, and folding margins in is what produced the variants.
    expect(body).not.toMatch(/^\s*(margin|padding|display)/m);
  });
});

describe('.muted', () => {
  const body = ruleBody(appCss, '.muted');

  it('is defined once, and changes colour only', () => {
    expect(body).toBeDefined();
    expect(body).toContain('color: var(--text-muted)');
    // No font-size: `.muted` de-emphasizes text *at whatever size it already
    // is* — that is the whole difference from .caption. A size here would
    // shrink every span it is dropped onto.
    expect(body).not.toMatch(/font-size/);
  });

  it('is typography only', () => {
    expect(body).not.toMatch(/^\s*(margin|padding|display)/m);
  });
});

describe('no page re-declares a role', () => {
  const files = svelteFiles();

  it('finds the tree', () => {
    expect(files.length).toBeGreaterThan(100);
  });

  for (const cls of ['caption', 'muted'] as const) {
    it(`no .svelte file restates .${cls}'s typography`, () => {
      // A page MAY declare the class to place it — a margin, a grid span.
      // That is the half of the contract that stays at the call site, and
      // forbidding it outright is what forced pages to re-fork the whole rule
      // just to move one. What it may not do is restate the colour or the
      // size, because then there are two answers to what the role looks like
      // and the page's wins silently.
      const offenders: string[] = [];
      for (const f of files) {
        for (const body of bareRuleBodies(styleBlock(readFileSync(f, 'utf8')), cls)) {
          if (/(^|\s)(color|font-size)\s*:/.test(body)) {
            offenders.push(f.slice(f.indexOf('src/')));
          }
        }
      }
      expect(offenders, `.${cls}'s typography belongs in app.css only`).toEqual([]);
    });
  }

  it('.hint survives only as the settings-scoped variant', () => {
    // `.settings .hint` is a genuinely different rule — --text-sm on
    // --text-muted with a 60ch measure, sized for a form's explanatory line
    // rather than a caption. It stays in settings.css, scoped, where its
    // specificity says so. What must not come back is a page re-declaring the
    // caption under this name.
    const offenders = files
      .filter((f) => declaresRule(styleBlock(readFileSync(f, 'utf8')), 'hint'))
      .map((f) => f.slice(f.indexOf('src/')));
    expect(offenders).toEqual([]);
  });

  it('.meta survives only where it means layout, not supporting text', () => {
    const offenders = files
      .filter((f) => declaresRule(styleBlock(readFileSync(f, 'utf8')), 'meta'))
      .map((f) => f.slice(f.indexOf('src/') + 4))
      .filter((f) => !META_IS_LAYOUT.includes(f));
    expect(offenders, 'use .caption for supporting text').toEqual([]);
  });
});
