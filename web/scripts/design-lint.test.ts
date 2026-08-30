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

  // app.css was one file until it was split into four layers behind
  // @imports. The literals did not move out of the token home — the token
  // home became four files — so each layer keeps the exemption the single
  // file had. Miss one and the split reports 82 "violations" that are the
  // palette itself.
  it.each([
    'src/lib/styles/tokens.css',
    'src/lib/styles/primitives.css',
    'src/lib/styles/app-shell.css',
    'src/lib/styles/markdown.css',
  ])('does not lint %s, a layer of the token home', (file) => {
    expect(rules('.x { color: #abc; }', file)).toHaveLength(0);
  });

  it('still lints the module sheets, which are not the token home', () => {
    // settings.css / cards.css / sidebar.css were always separate files and
    // were always linted. The split must not quietly widen the exemption to
    // everything under lib/styles.
    expect(rules('.x { color: #abc; }', 'src/lib/styles/settings.css')).toContain('raw-color');
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

  // `prompt` was outside the rule for as long as the rule existed, which is how
  // the feeds category control kept a native input dialog while the lint stayed
  // green over that file. It is the awkward one of the three: `confirm` and
  // `alert` are only ever the dialog here, where `prompt` is also an ordinary
  // identifier a Svelte snippet can carry.
  it('flags window.prompt', () => {
    expect(rules('const slug = window.prompt("slug?");')).toContain('native-dialog');
  });

  it('flags a bare prompt call', () => {
    expect(rules('const slug = prompt("New category slug:");')).toContain('native-dialog');
  });

  it('allows a local function named prompt', () => {
    expect(rules('function prompt() { open = true; }')).not.toContain('native-dialog');
  });

  it('allows a snippet declaration named prompt', () => {
    expect(rules('{#snippet prompt()}')).not.toContain('native-dialog');
  });

  it('allows rendering a snippet named prompt', () => {
    expect(rules('{@render prompt()}')).not.toContain('native-dialog');
  });

  // The discriminating case for how the snippet markers are excluded. A
  // whole-line `unless` would clear the line entirely and let the real dialog
  // through with it; blanking only the marker and the name it declares leaves
  // the dialog reportable.
  it('still flags a real prompt sharing a line with a snippet render', () => {
    expect(rules('{@render prompt()}{prompt("slug?")}')).toContain('native-dialog');
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

describe('off-scale-space', () => {
  it.each(['0.3rem', '0.35rem', '0.6rem', '0.9rem'])('flags %s', (value) => {
    expect(rules(styled(`padding: ${value};`))).toContain('off-scale-space');
  });

  it.each(['0.25rem', '0.5rem', '0.75rem', '1rem', '1.5rem', '2rem'])(
    'allows %s, which is on the ramp',
    (value) => {
      expect(rules(styled(`padding: ${value};`))).not.toContain('off-scale-space');
    },
  );

  it('allows a token', () => {
    expect(rules(styled('gap: var(--space-2);'))).not.toContain('off-scale-space');
  });

  it('checks every value in a shorthand', () => {
    const found = lint(styled('padding: 0.3rem 0.5rem 0.9rem 1rem;')).filter(
      (v) => v.rule === 'off-scale-space',
    );
    expect(found).toHaveLength(2);
  });

  it.each([
    ['px, which is right for a border', 'padding: 3px;'],
    ['em, which is relative to its own control', 'padding: 0.4em;'],
    ['percentages, which are layout', 'margin: 10%;'],
    ['arithmetic, whose operands are not independently meaningful', 'padding: calc(1rem - 2px);'],
    ['a clamp', 'gap: clamp(0.3rem, 2vw, 1rem);'],
  ])('allows %s', (_label, decl) => {
    expect(rules(styled(decl))).not.toContain('off-scale-space');
  });

  it('ignores non-spacing properties', () => {
    // A type size or a radius is not on this ramp and has its own tokens.
    expect(rules(styled('font-size: 0.85rem;', 'width: 3.5rem;'))).not.toContain('off-scale-space');
  });

  it('does not lint app.css, which defines the ramp', () => {
    expect(rules(styled('padding: 0.3rem;'), 'src/app.css')).toHaveLength(0);
  });

  it.each([
    'src/lib/styles/tokens.css',
    'src/lib/styles/primitives.css',
    'src/lib/styles/app-shell.css',
    'src/lib/styles/markdown.css',
  ])('does not lint %s, a layer of the same file', (file) => {
    expect(rules(styled('padding: 0.3rem;'), file)).toHaveLength(0);
  });
});

describe('redefined-primitive', () => {
  it.each(['.btn', '.badge', '.icon-btn', '.field', '.chip'])('flags %s', (cls) => {
    expect(rules(`<style>\n  ${cls} {\n    color: red;\n  }\n</style>`)).toContain(
      'redefined-primitive',
    );
  });

  it('flags a modifier on a primitive class', () => {
    expect(rules('<style>\n  .btn.danger {\n    color: red;\n  }\n</style>')).toContain(
      'redefined-primitive',
    );
  });

  it('flags a :global() escape of one', () => {
    expect(
      rules('<style>\n  :global(.field.full) {\n    grid-column: 1;\n  }\n</style>'),
    ).toContain('redefined-primitive');
  });

  it('allows the component that owns the class', () => {
    expect(
      rules(
        '<style>\n  .btn {\n    color: red;\n  }\n</style>',
        'src/lib/components/ui/Button.svelte',
      ),
    ).not.toContain('redefined-primitive');
  });

  it('allows a descendant-scoped placement rule', () => {
    // `.form :global(.field.full)` places an instance inside one page; it does
    // not redefine the primitive, and it cannot leak out of that page.
    expect(
      rules('<style>\n  .form :global(.field.full) {\n    grid-column: 1;\n  }\n</style>'),
    ).not.toContain('redefined-primitive');
  });

  it('allows an unrelated class that merely starts the same way', () => {
    expect(rules('<style>\n  .btn-group {\n    display: flex;\n  }\n</style>')).not.toContain(
      'redefined-primitive',
    );
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
