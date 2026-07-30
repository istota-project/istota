import { describe, expect, it } from 'vitest';
// @ts-expect-error — plain .mjs with no types; the rules are the unit under test.
import { RULES, scanSource } from './design-lint-rules.mjs';

// The lint's failure mode is silence: a regex that stops matching reports a
// clean tree, which is exactly what it looked like while 87 color literals in
// rgba()/hsla() notation sat in the source. These tests pin each rule to a
// source it must flag and a source it must not.

type Violation = { file: string; line: number; rule: string; text: string; hint: string };

/** The roster the token rules resolve against, standing in for app.css. */
const TOKENS = new Set(['--text-xs', '--text-base', '--surface-card', '--status-danger-fg']);

/** Lint a snippet as if it were a component under src/. */
function lint(source: string, rel = 'src/routes/x/+page.svelte'): Violation[] {
  return scanSource(rel, source, { tokens: TOKENS }) as Violation[];
}

function rules(source: string, rel?: string): string[] {
  return lint(source, rel).map((v) => v.rule);
}

/** Wrap declarations in a style block, which is where most rules look. */
function styled(...declarations: string[]): string {
  return `<style>\n  .x {\n    ${declarations.join('\n    ')}\n  }\n</style>`;
}

describe('rule table', () => {
  it('every rule has an id and a hint', () => {
    for (const rule of RULES as { id: string; hint: string }[]) {
      expect(rule.id, JSON.stringify(rule)).toBeTruthy();
      expect(rule.hint, rule.id).toBeTruthy();
    }
  });
});

describe('raw-color', () => {
  it.each([
    ['hex', 'color: #ff0044;'],
    ['short hex', 'color: #f04;'],
    ['hex with alpha', 'color: #ff004488;'],
    ['rgb', 'color: rgb(255, 0, 68);'],
    ['rgba', 'background: rgba(204, 102, 102, 0.15);'],
    ['rgb space syntax', 'background: rgb(0 0 0 / 0.6);'],
    ['hsl', 'background: hsl(0, 60%, 55%);'],
    ['hsla', 'background: hsla(0, 60%, 55%, 0.28);'],
    ['no space before paren', 'background:rgba(0,0,0,.5);'],
    ['space before paren', 'background: rgba (0, 0, 0, 0.5);'],
    ['inside a shadow', 'box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);'],
    ['inside a JS string', "borderColor: 'rgba(204, 102, 102, 0.5)',"],
  ])('flags %s', (_label, line) => {
    expect(rules(`<style>\n  .x {\n    ${line}\n  }\n</style>`)).toContain('raw-color');
  });

  it.each([
    ['a token', 'color: var(--status-danger-fg);'],
    ['an alpha derived from a token', 'background: rgb(var(--accent-rgb) / 0.5);'],
    ['relative color syntax over a token', 'background: rgb(from var(--accent) r g b / 0.4);'],
    ['an HTML numeric entity', '<span>&#9654;</span>'],
    ['a hex named in a line comment', '// the old value was #c66'],
    ['a hex named in a block comment', '/* was #c66 dark / #c0271d light */'],
  ])('allows %s', (_label, line) => {
    expect(rules(`<style>\n  .x {\n    ${line}\n  }\n</style>`)).not.toContain('raw-color');
  });

  it('still flags a literal sharing a line with a token-derived alpha', () => {
    // `unless` is whole-line, so this is what the `strip` hook exists for: the
    // sanctioned form must not launder the literal beside it.
    const line = 'box-shadow: 0 0 0 1px rgb(var(--x) / 0.2), 0 2px 4px rgba(0, 0, 0, 0.4);';
    expect(rules(`<style>\n  .x {\n    ${line}\n  }\n</style>`)).toContain('raw-color');
  });

  it('reports the line number', () => {
    const found = lint('<style>\n  .x {\n    color: #abc;\n  }\n</style>');
    expect(found).toHaveLength(1);
    expect(found[0].line).toBe(3);
    expect(found[0].rule).toBe('raw-color');
  });

  it('does not lint app.css, which is the token home', () => {
    expect(rules('.x { color: #abc; }', 'src/app.css')).toHaveLength(0);
  });

  it('does not lint tests, whose fixtures are the literals', () => {
    expect(rules("expect(meta).toBe('#111111');", 'src/lib/theme.test.ts')).toHaveLength(0);
  });
});

describe('allow comments', () => {
  it('an inline allow covers the next line carrying code', () => {
    const source = `<style>
  .x {
    /* design-lint-allow: fixed chrome — a scrim. */
    background: rgba(0, 0, 0, 0.5);
    background: rgba(0, 0, 0, 0.6);
  }
</style>`;
    // Covers the first declaration only; the second is still reported.
    expect(rules(source)).toEqual(['raw-color']);
  });

  it('an allow reaches past blank and comment lines to the code', () => {
    const source = `<style>
  .x {
    /* design-lint-allow: fixed chrome. */

    /* a further note */
    background: rgba(0, 0, 0, 0.5);
  }
</style>`;
    expect(rules(source)).toHaveLength(0);
  });

  it('a begin/end region covers everything between the markers', () => {
    const source = `<style>
  .x {
    /* design-lint-allow-begin: data viz. */
    background: rgba(0, 0, 0, 0.5);
    border-color: #abc;
    /* design-lint-allow-end */
    color: #def;
  }
</style>`;
    expect(rules(source)).toEqual(['raw-color']);
  });

  it('a file-level allow covers the whole file', () => {
    const source = `<!-- design-lint-allow-file: a palette. -->
<style>
  .x {
    color: #abc;
    background: rgba(0, 0, 0, 0.5);
  }
</style>`;
    expect(rules(source)).toHaveLength(0);
  });
});

describe('theme-override', () => {
  it('flags a hand-written light-theme rule', () => {
    expect(rules(":global(:root[data-theme='light']) .card { color: red; }")).toContain(
      'theme-override',
    );
  });
});

describe('native-dialog', () => {
  it('flags window.confirm', () => {
    expect(rules('if (window.confirm("sure?")) remove();')).toContain('native-dialog');
  });

  it('flags a bare confirm call', () => {
    expect(rules('if (confirm("sure?")) remove();')).toContain('native-dialog');
  });

  it('allows a local function named confirm', () => {
    expect(rules('function confirm() { open = true; }')).not.toContain('native-dialog');
  });

  it('allows a method named confirm', () => {
    expect(rules('await api.confirm(taskId);')).not.toContain('native-dialog');
  });
});

describe('deep-import', () => {
  it('flags a default import by file path', () => {
    expect(rules("import Button from '$lib/components/ui/Button.svelte';")).toContain(
      'deep-import',
    );
  });

  it('allows the barrel', () => {
    expect(rules("import { Button } from '$lib/components/ui';")).not.toContain('deep-import');
  });

  it('allows a named import the barrel does not re-export', () => {
    expect(
      rules("import { getShellScrollRoot } from '$lib/components/ui/AppShell.svelte';"),
    ).not.toContain('deep-import');
  });
});

describe('undefined-token', () => {
  it('flags a bare var() naming a token nothing defines', () => {
    const found = lint(styled('font-size: var(--text-lg);'));
    expect(found.map((v) => v.rule)).toEqual(['undefined-token']);
    expect(found[0].hint).toContain('--text-lg');
  });

  it('allows a bare var() naming a token in the roster', () => {
    expect(rules(styled('font-size: var(--text-xs);'))).toHaveLength(0);
  });

  it('reports a written fallback separately, because it renders', () => {
    // This is the whole point of the split: six of the --text-lg references
    // carried a fallback and looked right; six did not and shipped broken.
    expect(rules(styled('font-size: var(--text-lg, 1.05rem);'))).toEqual([
      'undefined-token-fallback',
    ]);
  });

  it('reports each undefined name on a line separately', () => {
    const found = lint(styled('box-shadow: 0 0 0 var(--ring-w) var(--ring-c);'));
    expect(found).toHaveLength(2);
    expect(found.map((v) => v.hint.split(':')[0])).toEqual(['--ring-w', '--ring-c']);
  });

  it('counts a property the file defines itself', () => {
    expect(rules(styled('--card-pad: 1rem;', 'padding: var(--card-pad);'))).toHaveLength(0);
  });

  it('counts a property set by a Svelte style: directive', () => {
    const source = `<div class="pane" style:--composer-h="{h}px"></div>
${styled('padding-bottom: var(--composer-h);')}`;
    expect(rules(source)).toHaveLength(0);
  });

  it('stands down when no roster is supplied', () => {
    expect(
      (scanSource('src/x/+page.svelte', styled('font-size: var(--nope);')) as Violation[]).filter(
        (v) => v.rule.startsWith('undefined-token'),
      ),
    ).toHaveLength(0);
  });
});

describe('comment stripping', () => {
  it('does not treat a /* inside an attribute value as a comment opener', () => {
    // `accept="image/*,application/pdf"` used to open a block comment that
    // never closed, blanking the rest of the file for every rule — four upload
    // pages were silently exempt, including a broken heading.
    const source = `<input accept="image/*,application/pdf" />
${styled('color: #abc;')}`;
    expect(rules(source)).toEqual(['raw-color']);
  });

  it('still strips a real block comment spanning lines', () => {
    const source = `<style>
  /* the old value was
     #c66, replaced by a token */
  .x {
    color: var(--status-danger-fg);
  }
</style>`;
    expect(rules(source)).toHaveLength(0);
  });

  it('recovers after a closed block comment', () => {
    const source = `<style>
  /* #c66 */
  .x {
    color: #abc;
  }
</style>`;
    expect(rules(source)).toEqual(['raw-color']);
  });
});

describe('stray-money-global', () => {
  it('flags a money shell class defined outside the money layout', () => {
    expect(rules(':global(.money-table-row) { padding: 0; }')).toContain('stray-money-global');
  });

  it('allows it in the layout that owns the shell', () => {
    expect(
      rules(':global(.money-table-row) { padding: 0; }', 'src/routes/money/+layout.svelte'),
    ).not.toContain('stray-money-global');
  });
});
