import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { compile } from 'svelte/compiler';
import { layoutMasonry, masonry } from './masonry';

const card = (height: number, width: 'auto' | 'wide' | 'full' = 'auto') => ({ height, width });
afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('masonry placement', () => {
  it('packs different heights into the shortest column without shrinking content', () => {
    expect(layoutMasonry([card(100), card(200), card(90)], 2, 16)).toEqual([
      { column: 0, span: 1, top: 0, height: 100 },
      { column: 1, span: 1, top: 0, height: 216 },
      { column: 0, span: 1, top: 116, height: 100 },
    ]);
  });
  it('snaps similar cards at the same level and moves following cards below the taller height', () => {
    const placed = layoutMasonry([card(180), card(196), card(80)], 2, 16);
    expect(placed.map((p) => [p.top, p.height])).toEqual([
      [0, 200],
      [0, 200],
      [216, 100],
    ]);
  });
  it('limits each snap group rather than chaining small differences into a large stretch', () => {
    expect(layoutMasonry([card(100), card(140), card(190)], 3, 16).map((p) => p.height)).toEqual([
      150, 150, 200,
    ]);
  });
  it('honors wide and full cards, including a single column', () => {
    const cards = [card(100, 'wide'), card(180), card(70, 'full'), card(90)];
    expect(layoutMasonry(cards, 3, 16)).toEqual([
      { column: 0, span: 2, top: 0, height: 100 },
      { column: 2, span: 1, top: 0, height: 200 },
      { column: 0, span: 3, top: 216, height: 100 },
      { column: 0, span: 1, top: 332, height: 100 },
    ]);
    expect(layoutMasonry(cards, 1, 16).map((p) => [p.column, p.span, p.top])).toEqual([
      [0, 1, 0],
      [0, 1, 116],
      [0, 1, 332],
      [0, 1, 448],
    ]);
  });
  it('matches neighboring cards across a height step without excessive stretching', () => {
    expect(layoutMasonry([card(226), card(253)], 2, 16).map((p) => p.height)).toEqual([300, 300]);
    expect(layoutMasonry([card(151), card(249)], 2, 16).map((p) => p.height)).toEqual([200, 250]);
  });
  it('fills a small gap before a spanning block when neighboring cards start at different heights', () => {
    const placed = layoutMasonry(
      [card(400, 'wide'), card(200), card(350), card(200, 'wide'), card(100, 'full')],
      3,
      16,
    );
    const tasks = placed[2];
    const newsletters = placed[3];
    expect(tasks.top).not.toBe(newsletters.top);
    expect(tasks.top + tasks.height).toBe(newsletters.top + newsletters.height);
    expect(tasks.height).toBe(400);
    expect(placed[4].top).toBe(newsletters.top + newsletters.height + 16);
  });
  it('does not stretch across an intervening card or a gap larger than one step', () => {
    const placed = layoutMasonry(
      [card(400, 'wide'), card(100), card(100), card(100, 'full')],
      3,
      16,
    );
    expect(placed[1].height).toBe(100);
    expect(placed[2].height).toBe(100);
  });
  it('snaps a card to a neighbor bottom within a step even when they start at different tops', () => {
    const groups = (rooms: number) =>
      layoutMasonry([card(650, 'wide'), card(396), card(rooms), card(264)], 3, 24);
    const bottom = (p: { top: number; height: number }) => p.top + p.height;

    const shorter = groups(220);
    expect(bottom(shorter[2])).toBe(bottom(shorter[0]));
    expect(shorter[0].height).toBe(650);

    const longer = groups(250);
    expect(bottom(longer[0])).toBe(bottom(longer[2]));
    expect(longer[2].height).toBe(250);
    expect(longer[3].top).toBe(bottom(longer[0]) + 24);
  });
  it('handles an empty edition', () => {
    expect(layoutMasonry([], 2, 16)).toEqual([]);
  });
});

it('remeasures intrinsic content and responds to column changes without height feedback', () => {
  let resize!: ResizeObserverCallback;
  const disconnect = vi.fn();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: ResizeObserverCallback) {
        resize = callback;
      }
      observe() {}
      disconnect = disconnect;
    },
  );
  let frame: FrameRequestCallback | undefined;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frame = callback;
    return 1;
  });
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  const flush = () => {
    const callback = frame;
    frame = undefined;
    callback?.(0);
  };
  const grid = document.createElement('div');
  grid.style.gridTemplateColumns = '200px 200px';
  grid.style.columnGap = '16px';
  let heights = [180, 196, 80];
  for (let i = 0; i < 3; i++) {
    const item = document.createElement('section');
    item.dataset.width = 'auto';
    const content = document.createElement('div');
    content.dataset.masonryContent = '';
    content.getBoundingClientRect = () => ({ height: heights[i] }) as DOMRect;
    item.append(content);
    grid.append(item);
  }
  document.body.append(grid);
  const action = masonry(grid);
  flush();
  const items = [...grid.children] as HTMLElement[];
  expect(items[0].style.minHeight).toBe('200px');
  expect(items[2].style.gridRowStart).toBe('217');
  heights = [260, 196, 80];
  resize([], {} as ResizeObserver);
  flush();
  expect(items[0].style.minHeight).toBe('300px');
  expect(items[1].style.minHeight).toBe('200px');
  expect(items[2].style.gridColumnStart).toBe('2');
  grid.style.gridTemplateColumns = '400px';
  resize([], {} as ResizeObserver);
  flush();
  expect(items[1].style.gridRowStart).toBe('317');
  expect(items[2].style.gridColumnStart).toBe('1');
  disconnect.mockClear();
  action.destroy();
  expect(disconnect).toHaveBeenCalledOnce();
});

it('measures a card without a content wrapper itself, after clearing its snapped height', () => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  let frame: FrameRequestCallback | undefined;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frame = callback;
    return 1;
  });
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  const flush = () => {
    const callback = frame;
    frame = undefined;
    callback?.(0);
  };
  const grid = document.createElement('div');
  grid.style.gridTemplateColumns = '200px 200px';
  grid.style.columnGap = '16px';
  const intrinsic = [180, 196];
  for (let i = 0; i < 2; i++) {
    const card = document.createElement('section');
    card.style.padding = '16px';
    // A snapped min-height would read back as the card's height; the action
    // must clear it before measuring, or a card could never shrink.
    card.getBoundingClientRect = () =>
      ({ height: Math.max(intrinsic[i], parseFloat(card.style.minHeight) || 0) }) as DOMRect;
    grid.append(card);
  }
  document.body.append(grid);
  const action = masonry(grid);
  flush();
  const cards = [...grid.children] as HTMLElement[];
  // Padding is already inside the card's own box, so it is not added again.
  expect(cards[0].style.minHeight).toBe('200px');
  intrinsic[0] = 60;
  intrinsic[1] = 60;
  cards[0].append(document.createTextNode('changed'));
  return new Promise<void>((resolve) => {
    setTimeout(() => {
      flush();
      expect(cards[0].style.minHeight).toBe('100px');
      action.destroy();
      grid.remove();
      resolve();
    });
  });
});

it('keeps the runtime masonry row rules in the compiled card grid CSS', () => {
  const filename = 'src/lib/components/ui/CardGrid.svelte';
  const source = readFileSync(filename, 'utf8');
  const result = compile(source, { filename, generate: 'client', css: 'external' });
  expect(result.css?.code).toMatch(/grid-auto-rows:\s*1px/);
  expect(result.css?.code).toMatch(/row-gap:\s*0/);
});
