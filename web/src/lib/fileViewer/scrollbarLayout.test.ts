import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

function rule(path: string, selector: string): string {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const css = source.slice(source.indexOf('<style>'));
  const start = css.indexOf(`${selector} {`);
  expect(start, `Missing ${selector}`).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf('}', start) + 1);
}

describe('file viewer scrollbar layout', () => {
  const text = '../components/viewer/TextPreview.svelte';

  it('keeps padding inside the rendered text scroller', () => {
    expect(rule(text, '.fill .file-body')).toContain('overflow: auto;');
    expect(rule(text, '.fill .markdown')).toContain('padding: var(--space-2) var(--space-3);');
  });

  it('lets the source textarea reach the edge with its own content padding', () => {
    const textarea = rule(text, '.fill :global(textarea)');
    expect(textarea).toContain('padding: var(--space-2) var(--space-3);');
    expect(textarea).toContain('border: 0;');
    expect(textarea).toContain('border-radius: 0;');
  });

  it('keeps PDF padding inside the scroller and clears the page controls', () => {
    const scroll = rule('./PdfPreview.svelte', '.pdf-scroll');
    expect(scroll).toContain('overflow: auto;');
    expect(scroll).toContain('padding: var(--space-2) var(--space-3);');
    expect(scroll).toContain('padding-bottom: calc(var(--pdf-controls-height) + var(--space-4));');
  });
});
