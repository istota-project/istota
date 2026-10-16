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

  it('leaves placement to CSS when masonry is off and no card asks to fill', () => {
    vi.stubGlobal('requestAnimationFrame', (run: FrameRequestCallback) => (run(0), 1));
    const { container } = render(CardGrid, { children: cards });
    const tracks = tracksOf(container);
    expect(tracks.classList.contains('masonry')).toBe(false);
    for (const card of tracks.children) {
      expect((card as HTMLElement).style.gridColumn).toBe('');
    }
  });

  it('sets the minimum ratio only when one is given', () => {
    const plain = render(CardGrid, { children: cards });
    expect(tracksOf(plain.container).classList.contains('aspect')).toBe(false);
    cleanup();
    const square = render(CardGrid, { minAspectRatio: 1, children: cards });
    const tracks = tracksOf(square.container);
    expect(tracks.classList.contains('aspect')).toBe(true);
    expect(tracks.style.getPropertyValue('--card-grid-ratio')).toBe('1');
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
    expect(block).toContain(`--card-grid-cols: ${count};`);
  });

  it('holds a card to the minimum ratio through its height', () => {
    expect(css).toMatch(
      /\.aspect[^>{]*> \*[^{]*\{ min-height: calc\(var\(--card-grid-track\) \/ var\(--card-grid-ratio\)\);/,
    );
  });

  it('spans a wide card only where two columns exist', () => {
    const wide = css.indexOf("[data-width='wide']");
    expect(wide).toBeGreaterThan(css.indexOf('@container card-grid (min-width: 40rem)'));
    expect(css).toMatch(/\[data-width='full'\][^{]*\{ grid-column: 1 \/ -1;/);
  });
});
