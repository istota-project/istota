import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { readCascade } from './cascade';

/**
 * The supporting-text roles: `.caption`, `.muted`, `.description`, and their
 * uppercase sibling `.micro-label`.
 *
 * All four existed everywhere and were named nowhere. "Small dim text" was
 * spelled `.hint` on four pages, `.meta` on five more and `.small` on two, and
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
 * the copies, and it belongs at the call site — that the roles keep the
 * distinct tokens that make them separate roles, and that no rule anywhere
 * else writes a role's typography out again under a name of its own.
 *
 * Same reasoning as contentFrame.test.ts and controlTier.test.ts.
 */

const appCss = readCascade();
const SRC = join(process.cwd(), 'src');

/** The body of a top-level rule, by selector. */
const ruleBody = (css: string, selector: string): string | undefined => {
  const at = css.indexOf(`\n${selector} {`);
  if (at === -1) return undefined;
  return css.slice(at, css.indexOf('}', at));
};

/** Every file under src/ with one of the given extensions. */
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

const stripComments = (css: string): string =>
  css.replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length));

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
 * The declarations a rule may carry beside a role's typography and still be a
 * copy of the role: where the text sits, not what it looks like. A rule that
 * also draws a surface — a border, a fill, a button reset — is a component that
 * happens to share the type (a pill, a sortable header, a tab), and the role
 * classes are not for those.
 */
const PLACEMENT =
  /^(margin|padding|display|gap|row-gap|column-gap|grid|flex|align-|justify-|place-|order$|width$|min-width$|max-width$|white-space$|overflow|text-overflow$|text-align$|vertical-align$|font-variant-numeric$|line-height$)/;

interface Role {
  name: string;
  matches: (decls: Record<string, string>) => boolean;
  /** The typography the role owns, which a copy may carry any of. */
  owns: string[];
}

/**
 * What a copy of each role looks like, by declaration. Checked in this order,
 * so an uppercase run is a micro-label and never also a caption.
 *
 * The caption shape takes --text-muted as well as --text-dim: "extra-small and
 * muted" was folded into `.caption` rather than kept as a second small role,
 * so a rule writing it is writing a caption one step off.
 */
const ROLES: Role[] = [
  {
    name: 'micro-label',
    matches: (d) =>
      d['text-transform'] === 'uppercase' &&
      d['letter-spacing'] === 'var(--tracking-caps)' &&
      d['font-size'] === 'var(--text-xs)',
    owns: ['color', 'font-size', 'font-weight', 'letter-spacing', 'text-transform'],
  },
  {
    name: 'caption',
    matches: (d) =>
      d['font-size'] === 'var(--text-xs)' &&
      (d.color === 'var(--text-dim)' || d.color === 'var(--text-muted)'),
    owns: ['color', 'font-size'],
  },
  {
    name: 'description',
    matches: (d) => d['font-size'] === 'var(--text-sm)' && d.color === 'var(--text-muted)',
    owns: ['color', 'font-size'],
  },
];

interface Copy {
  role: string;
  selector: string;
}

/**
 * Every rule in `css` that writes out a role's typography under a selector of
 * its own, with nothing beside it but placement. Innermost rules only, so a
 * rule inside an `@media` block is read on its own.
 */
export function roleCopies(css: string): Copy[] {
  const out: Copy[] = [];
  for (const m of stripComments(css).matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
    const selector = m[1]
      .replace(/<style[^>]*>/, '')
      .trim()
      .replace(/\s+/g, ' ');
    const decls: Record<string, string> = {};
    for (const part of m[2].split(';')) {
      const colon = part.indexOf(':');
      if (colon === -1) continue;
      decls[part.slice(0, colon).trim()] = part.slice(colon + 1).trim();
    }
    for (const role of ROLES) {
      if (!role.matches(decls)) continue;
      if (selector === `.${role.name}`) break;
      const rest = Object.keys(decls).filter((p) => !role.owns.includes(p) && !PLACEMENT.test(p));
      if (rest.length === 0) out.push({ role: role.name, selector });
      break;
    }
  }
  return out;
}

/**
 * Chat's reading surface keeps its own type, by decision: the message row, a
 * received mail, a draft, the activity chain and the composer are tuned as one
 * reading column, and folding their small type onto the app's roles would be a
 * redesign of that column rather than a consolidation.
 */
const READING_SURFACE = [
  'lib/components/chat/Message.svelte',
  'lib/components/chat/MailCard.svelte',
  'lib/components/chat/DraftCard.svelte',
  'lib/components/chat/ActivityTrace.svelte',
  'lib/components/chat/Composer.svelte',
];

/**
 * Column headers, which write the micro-label type because a `<th>` cannot
 * take a class from a stylesheet. Each goes when the table family lands and
 * `.table thead th` carries the type once, in table.css; until then a header
 * here is a known copy rather than a new one.
 */
const TABLE_HEADERS = [
  'lib/styles/settings.css|.settings .grid th',
  'routes/health/+layout.svelte|.health-frame :global(table.grid th)',
  'routes/health/labs/marker/+page.svelte|.history th',
];

/** Where role copies may not be written: every stylesheet but the role's own. */
const scanned = (): { file: string; css: string }[] => {
  const out: { file: string; css: string }[] = [];
  for (const f of sourceFiles('.svelte', '.css')) {
    const file = rel(f);
    if (file === 'lib/styles/primitives.css' || file === 'lib/styles/tokens.css') continue;
    const source = readFileSync(f, 'utf8');
    out.push({ file, css: f.endsWith('.svelte') ? styleBlock(source) : source });
  }
  return out;
};

/**
 * `.meta` is the one name the migration deliberately left overloaded. Three
 * files use it for something that is not supporting text at all — the chat
 * message's author/timestamp row, the labs marker's stat row, and the
 * encounter page's card surface — and folding a layout rule into a typography
 * global would say they were the same thing. They keep the name locally;
 * a Svelte-scoped rule outranks a global, so nothing collides.
 */
const META_IS_LAYOUT = [
  'lib/components/chat/Message.svelte',
  'routes/health/labs/marker/+page.svelte',
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

  it('is one colour everywhere, with no scope redefining it', () => {
    // settings.css used to restate it on --text-dim under `.settings`, so the
    // same class was #666 on every settings and admin page and #888 everywhere
    // else. The per-theme value is the token's, in tokens.css.
    expect(stripComments(appCss)).not.toMatch(/[\w)\]-]\s+\.muted\s*\{/);
  });
});

describe('.description', () => {
  const body = ruleBody(appCss, '.description');

  it('is defined once: the small step, on the muted token, at a reading measure', () => {
    expect(body).toBeDefined();
    // One step up from a caption in both size and colour, because it is read
    // rather than glanced at: the line under a page, card or modal title.
    expect(body).toContain('color: var(--text-muted)');
    expect(body).toContain('font-size: var(--text-sm)');
    expect(body).toContain('max-width: 60ch');
  });

  it('carries no spacing', () => {
    expect(body).not.toMatch(/^\s*(margin|padding|display)/m);
  });

  it('replaced the settings-scoped .hint, so the settings sheet only places it', () => {
    expect(stripComments(appCss)).not.toMatch(/\.settings \.hint\b/);
    const placed = /\.settings :where\(\.description\)\s*\{([^}]*)\}/.exec(appCss);
    expect(placed).not.toBeNull();
    expect(placed![1]).not.toMatch(/(^|\s)(color|font-size)\s*:/);
  });
});

describe('no page re-declares a role', () => {
  const files = sourceFiles('.svelte');

  it('finds the tree', () => {
    expect(files.length).toBeGreaterThan(100);
  });

  for (const cls of ['caption', 'muted', 'description', 'micro-label'] as const) {
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
          if (/(^|\s)(color|font-size)\s*:/.test(body)) offenders.push(rel(f));
        }
      }
      expect(offenders, `.${cls}'s typography belongs in app.css only`).toEqual([]);
    });
  }

  it('.hint is not a class any page declares', () => {
    // It named two different roles — a caption on some pages and the
    // settings shell's description on others — so it was retired rather than
    // given to either.
    const offenders = files
      .filter((f) => declaresRule(styleBlock(readFileSync(f, 'utf8')), 'hint'))
      .map(rel);
    expect(offenders).toEqual([]);
  });

  it('.meta survives only where it means layout, not supporting text', () => {
    const offenders = files
      .filter((f) => declaresRule(styleBlock(readFileSync(f, 'utf8')), 'meta'))
      .map(rel)
      .filter((f) => !META_IS_LAYOUT.includes(f));
    expect(offenders, 'use .caption for supporting text').toEqual([]);
  });
});

describe('no rule writes a role out under another name', () => {
  describe('the detector', () => {
    it('finds a caption copy', () => {
      expect(roleCopies('.x { font-size: var(--text-xs); color: var(--text-dim) }')).toEqual([
        { role: 'caption', selector: '.x' },
      ]);
    });

    it('finds the extra-small muted spelling, which is a caption one step off', () => {
      expect(roleCopies('.x { color: var(--text-muted); font-size: var(--text-xs); }')).toEqual([
        { role: 'caption', selector: '.x' },
      ]);
    });

    it('finds a micro-label copy, whatever its colour', () => {
      const css = `.alert h3 {
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: var(--tracking-caps);
        color: var(--text-muted);
        margin: 0;
      }`;
      expect(roleCopies(css)).toEqual([{ role: 'micro-label', selector: '.alert h3' }]);
    });

    it('finds a description copy inside a media query', () => {
      const css = `@media (max-width: 640px) {
        .note { font-size: var(--text-sm); color: var(--text-muted); max-width: 40ch; }
      }`;
      expect(roleCopies(css)).toEqual([{ role: 'description', selector: '.note' }]);
    });

    it('lets placement through', () => {
      expect(roleCopies('.caption { margin: 0; }')).toEqual([]);
      expect(
        roleCopies('.note { margin-top: var(--space-1); font-size: var(--text-sm); }'),
      ).toEqual([]);
    });

    it('lets a component that only shares the type through', () => {
      // A pill draws a surface; the role classes are text, not chrome.
      const pill = `.pill {
        font-size: var(--text-xs);
        color: var(--text-muted);
        background: var(--surface-raised);
        border-radius: var(--radius-pill);
      }`;
      expect(roleCopies(pill)).toEqual([]);
    });

    it('reads past comments', () => {
      expect(
        roleCopies(
          '/* .x { font-size: var(--text-xs); color: var(--text-dim) } */ .y { margin: 0 }',
        ),
      ).toEqual([]);
    });
  });

  const files = scanned();

  it('scans the module sheets and the money shell as well as the components', () => {
    const names = files.map((f) => f.file);
    expect(names).toContain('lib/styles/settings.css');
    expect(names).toContain('lib/styles/moneyTable.css');
    expect(names).not.toContain('lib/styles/primitives.css');
    expect(files.length).toBeGreaterThan(100);
  });

  it('finds no copy outside primitives.css', () => {
    const offenders: string[] = [];
    for (const { file, css } of files) {
      if (READING_SURFACE.includes(file)) continue;
      for (const copy of roleCopies(css)) {
        if (TABLE_HEADERS.includes(`${file}|${copy.selector}`)) continue;
        offenders.push(`${file}: ${copy.selector} writes out .${copy.role}`);
      }
    }
    expect(offenders, 'put the role class on the element and keep only placement here').toEqual([]);
  });

  it('holds no stale exemption', () => {
    // An entry that no longer matches anything would silently admit the next
    // copy written under the same selector.
    for (const entry of TABLE_HEADERS) {
      const [file, selector] = entry.split('|');
      const found = files.find((f) => f.file === file);
      expect(found, entry).toBeDefined();
      expect(
        roleCopies(found!.css).some((c) => c.selector === selector),
        entry,
      ).toBe(true);
    }
    for (const file of READING_SURFACE) {
      expect(
        files.some((f) => f.file === file),
        file,
      ).toBe(true);
    }
  });
});
