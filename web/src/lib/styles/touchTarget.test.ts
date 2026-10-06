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

// Stage 2 removes these as their controls move onto the shared primitive.
const KNOWN_OVERLAYS = [
  '/lib/components/chat/Message.svelte: .send-queued :global(.icon-btn)::before',
  '/lib/components/chat/Message.svelte: .turn-action::before',
  '/lib/components/ui/NoticeDrawer.svelte: .notice-dismiss::before',
  '/lib/components/ui/SidebarToggle.svelte: .sidebar-toggle::before',
  '/lib/styles/markdown.css: .app-nav .nav-right .nav-icon-btn::before',
];

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
  it('finds exactly the known file and selector exceptions', () => {
    const found: string[] = [];
    for (const file of styleFiles(SRC)) {
      if (file === resolve(SRC, 'lib/styles/primitives.css')) continue;
      for (const block of styleBlocks(file, readFileSync(file, 'utf8'))) {
        for (const selector of handWrittenOverlays(block)) {
          found.push(`${file.slice(SRC.length)}: ${selector}`);
        }
      }
    }
    expect(found.sort()).toEqual([...KNOWN_OVERLAYS].sort());
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
