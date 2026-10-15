import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { readFileSync } from 'node:fs';
import { compile } from 'svelte/compiler';
import CardGrid from './CardGrid.svelte';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const cards = createRawSnippet(() => ({
  render: () => '<div><section data-width="wide">a</section><section>b</section></div>',
}));

function tracksOf(container: HTMLElement) {
  return container.querySelector<HTMLElement>('.card-grid-tracks')!;
}

describe('CardGrid', () => {
  it('caps at three columns unless told otherwise', () => {
    const { container } = render(CardGrid, { children: cards });
    expect(tracksOf(container).dataset.columns).toBe('3');
  });

  it('carries a five-column cap for the feed wall', () => {
    const { container } = render(CardGrid, { columns: 5, children: cards });
    expect(tracksOf(container).dataset.columns).toBe('5');
  });

  it('leaves placement to CSS when masonry is off', () => {
    const raf = vi.fn();
    vi.stubGlobal('requestAnimationFrame', raf);
    const { container } = render(CardGrid, { children: cards });
    expect(tracksOf(container).classList.contains('masonry')).toBe(false);
    expect(raf).not.toHaveBeenCalled();
  });

  it('runs the masonry layout when asked', () => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    );
    const raf = vi.fn(() => 1);
    vi.stubGlobal('requestAnimationFrame', raf);
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
    const { container } = render(CardGrid, { masonry: true, children: cards });
    expect(tracksOf(container).classList.contains('masonry')).toBe(true);
    expect(raf).toHaveBeenCalled();
  });
});

describe('CardGrid column steps', () => {
  const filename = 'src/lib/components/ui/CardGrid.svelte';
  const css = compile(readFileSync(filename, 'utf8'), {
    filename,
    generate: 'client',
    css: 'external',
  }).css!.code.replace(/\s+/g, ' ');

  it.each([
    ['40rem', 2],
    ['64rem', 3],
    ['86rem', 4],
    ['108rem', 5],
  ])('steps up at %s to %i columns', (width, count) => {
    const block = css.split(`@container card-grid (min-width: ${width})`)[1] ?? '';
    expect(block).toContain(`repeat(${count}, minmax(0, 1fr))`);
  });

  it('spans a wide card only where two columns exist', () => {
    const wide = css.indexOf("[data-width='wide']");
    expect(wide).toBeGreaterThan(css.indexOf('@container card-grid (min-width: 40rem)'));
    expect(css).toMatch(/\[data-width='full'\][^{]*\{ grid-column: 1 \/ -1;/);
  });
});
