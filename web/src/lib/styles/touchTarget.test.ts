import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { blockAfter, readLayer, rules, stripComments, styleBlocks, styleFiles } from './cascade';

const SRC = resolve(process.cwd(), 'src');
const primitives = stripComments(readLayer('primitives'));

function handWrittenOverlays(source: string): string[] {
  return rules(source)
    .filter(
      ({ selector, body }) =>
        selector.includes('::before') &&
        /position:\s*absolute\s*;/.test(body) &&
        /translate\(\s*-50%\s*,\s*-50%\s*\)/.test(body) &&
        /(?:width|height):\s*[^;]*\d(?:px|rem)\b/.test(body),
    )
    .map(({ selector }) => selector);
}

describe('shared touch target', () => {
  it('declares the AA floor and comfort size in the tokens layer', () => {
    const root = blockAfter(stripComments(readLayer('tokens')), ':root {') ?? '';
    expect(root).toMatch(/--touch-min:\s*24px;/);
    expect(root).toMatch(/--touch-comfort:\s*44px;/);
  });

  it('centres an out-of-flow overlay only on a coarse pointer', () => {
    // Collect every coarse block: the input font floor already has its own.
    let remaining = primitives;
    const coarse: string[] = [];
    const needle = '@media (pointer: coarse)';
    while (remaining.includes(needle)) {
      const at = remaining.indexOf(needle);
      const open = remaining.indexOf('{', at);
      const body = blockAfter(remaining, needle);
      expect(body).not.toBeNull();
      coarse.push(body!);
      remaining = remaining.slice(0, at) + remaining.slice(open + body!.length + 2);
    }
    expect(remaining).not.toContain('.touch-target');
    const targetRules = rules(coarse.join('\n')).filter((r) =>
      r.selector.includes('.touch-target'),
    );
    expect(targetRules.map((r) => r.selector)).toEqual(['.touch-target', '.touch-target::before']);
    expect(targetRules[0].body).toMatch(/position:\s*relative;/);
    const overlay = targetRules[1].body.replace(/\s+/g, '');
    for (const declaration of [
      "content:'';",
      'position:absolute;',
      'top:50%;',
      'left:50%;',
      'width:max(100%,var(--touch-target-w,var(--touch-min)));',
      'height:max(100%,var(--touch-target-h,var(--touch-min)));',
      'transform:translate(-50%,-50%);',
    ])
      expect(overlay).toContain(declaration);
  });
});

describe('hand-written overlay drift guard', () => {
  it('allows no hand-written overlays outside the primitive', () => {
    const found: string[] = [];
    for (const file of styleFiles(SRC)) {
      if (file === resolve(SRC, 'lib/styles/primitives.css')) continue;
      for (const block of styleBlocks(file, readFileSync(file, 'utf8'))) {
        for (const selector of handWrittenOverlays(block)) {
          found.push(`${file.slice(SRC.length)}: ${selector}`);
        }
      }
    }
    expect(found).toEqual([]);
  });

  it('recognizes both fixed sizes and gap-derived widths without matching decoration', () => {
    const overlay = 'position: absolute; transform: translate(-50%, -50%);';
    expect(
      handWrittenOverlays(`
      @media (pointer: coarse) {
        .fixed::before { ${overlay} width: 2.5rem; height: 44px; }
        .gap :global(.icon-btn)::before { ${overlay} width: calc(100% + var(--gap)); height: 44px; }
        .decoration::before { position: absolute; width: 44px; }
      }
    `),
    ).toEqual(['.fixed::before', '.gap :global(.icon-btn)::before']);
  });
});

describe('migrated touch targets', () => {
  it.each([
    ['lib/components/ui/SidebarToggle.svelte', '.sidebar-toggle', '2.5rem', '2.75rem'],
    [
      'lib/components/ui/NoticeDrawer.svelte',
      '.notice-dismiss',
      'var(--touch-comfort)',
      'var(--touch-comfort)',
    ],
    [
      'lib/components/chat/Message.svelte',
      '.turn-action',
      'calc(100% + var(--turn-action-gap))',
      'var(--touch-comfort)',
    ],
    [
      'lib/components/chat/Message.svelte',
      '.send-queued',
      'calc(100% + var(--space-2))',
      'var(--touch-comfort)',
    ],
    ['lib/styles/app-shell.css', '.app-nav .nav-right .nav-icon-btn', '2.5rem', '2.5rem'],
  ])('preserves %s %s dimensions', (file, selector, width, height) => {
    const source = readFileSync(resolve(SRC, file), 'utf8');
    const bodies = styleBlocks(file, source)
      .flatMap(rules)
      .filter((r) => r.selector === selector)
      .map((r) => r.body)
      .join('\n');
    expect(bodies).toContain(`--touch-target-w: ${width};`);
    expect(bodies).toContain(`--touch-target-h: ${height};`);
  });

  it('keeps the nav gap for coarse pointers and narrow windows in the shell layer', () => {
    const shell = stripComments(readLayer('app-shell'));
    const gap = blockAfter(shell, '@media (pointer: coarse), (max-width: 640px)') ?? '';
    expect(blockAfter(gap, '.app-nav .nav-right')).toMatch(/gap:\s*1.25rem;/);
    expect(readLayer('markdown')).not.toContain('.nav-icon-btn');
  });

  it.each([
    ['lib/components/ui/SidebarToggle.svelte', 'sidebar-toggle'],
    ['lib/components/ui/NoticeDrawer.svelte', 'notice-dismiss'],
    ['lib/components/chat/Message.svelte', 'turn-action'],
    ['lib/components/ui/NotificationBell.svelte', 'nav-icon-btn'],
    ['lib/components/LogoutButton.svelte', 'nav-icon-btn'],
    ['routes/+layout.svelte', 'nav-icon-btn'],
  ])('opts every %s %s call site into the primitive', (file, control) => {
    const source = readFileSync(resolve(SRC, file), 'utf8');
    const classes = [...source.matchAll(/class="([^"]+)"/g)]
      .map((m) => m[1].split(' '))
      .filter((names) => names.includes(control));
    expect(classes.length).toBeGreaterThan(0);
    for (const names of classes) expect(names).toContain('touch-target');
  });
});

describe('reported touch controls', () => {
  it.each([
    [
      'lib/components/ui/HeaderNav.svelte',
      '.nav-select',
      '--touch-target-h: var(--touch-comfort);',
    ],
    [
      'lib/components/ui/Select.svelte',
      ':global(.ui-select-item)',
      'min-height: var(--touch-comfort);',
    ],
    ['lib/components/ui/Select.svelte', ':global(.ui-select-viewport)', 'gap: 0;'],
    [
      'lib/components/ui/KebabMenu.svelte',
      ':global(.ui-kebab-trigger)',
      'min-width: var(--touch-min);',
    ],
    ['lib/styles/sidebar.css', '.sidebar .list-row', 'gap: var(--space-2);'],
    [
      'routes/feeds/+page.svelte',
      '.feed-grid :global(.star-btn)',
      '--touch-target-w: var(--touch-comfort);',
    ],
    [
      'routes/feeds/+page.svelte',
      '.feed-grid :global(.star-btn)',
      '--touch-target-h: var(--touch-comfort);',
    ],
  ])('gates %s %s %s on a coarse pointer', (file, selector, declaration) => {
    const source = stripComments(readFileSync(resolve(SRC, file), 'utf8'));
    const coarse = blockAfter(source, '@media (pointer: coarse)') ?? '';
    expect(blockAfter(coarse, selector)).toContain(declaration);
  });
});
