import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { blockAfter, readLayer, rules, stripComments, styleBlocks, styleFiles } from './cascade';

// The app half of minot's touchTarget test. The `.touch-target` primitive and
// the library components' targets are minot's; this holds the drift guard over
// this app's files and the app's own controls (chat, the app nav, feeds,
// health).

const SRC = resolve(process.cwd(), 'src');

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

describe('hand-written overlay drift guard', () => {
  it('allows no hand-written overlays outside the primitive', () => {
    const found: string[] = [];
    for (const file of styleFiles(SRC)) {
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
    ['lib/components/chat/Message.svelte', 'turn-action'],
    ['lib/components/notifications/NotificationBell.svelte', 'nav-icon-btn'],
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
      'routes/feeds/+page.svelte',
      '.feed-grid :global(.star-btn)',
      'padding-inline-end: var(--space-3);',
    ],
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

describe('compact control touch targets', () => {
  it.each([
    ['lib/components/chat/Composer.svelte', 'attach-x'],
    ['routes/health/documents/+page.svelte', 'detach'],
  ])('uses the shared target for %s %s', (file, control) => {
    const source = readFileSync(resolve(SRC, file), 'utf8');
    expect(source).toContain(`class="${control} touch-target"`);
  });

  it('asks IconButton for the shared target on the lab panel delete', () => {
    const source = readFileSync(resolve(SRC, 'routes/health/labs/panel/+page.svelte'), 'utf8');
    expect(source).toMatch(/class="del"\s+touchTarget/);
  });
});
