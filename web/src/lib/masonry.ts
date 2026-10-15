export interface MasonryItem {
  height: number;
  width: 'auto' | 'wide' | 'full';
}
export interface MasonryPlacement {
  column: number;
  span: number;
  top: number;
  height: number;
}

export function layoutMasonry(
  items: MasonryItem[],
  columns: number,
  gap: number,
  heightStep = 50,
): MasonryPlacement[] {
  const bottoms = Array<number>(columns).fill(0);
  const placed: MasonryPlacement[] = [];
  let pending: MasonryPlacement[] = [];

  function position(span: number) {
    let column = 0;
    let top = Infinity;
    for (let start = 0; start <= columns - span; start++) {
      const candidate = Math.max(...bottoms.slice(start, start + span));
      if (candidate < top) {
        column = start;
        top = candidate;
      }
    }
    return { column, top };
  }

  function settle() {
    const sorted = [...pending].sort((a, b) => a.height - b.height);
    for (let start = 0; start < sorted.length;) {
      let end = start + 1;
      while (end < sorted.length && sorted[end].height - sorted[start].height <= heightStep) end++;
      const height = Math.ceil(sorted[end - 1].height / heightStep) * heightStep;
      for (let i = start; i < end; i++) sorted[i].height = height;
      start = end;
    }
    for (const item of pending) {
      for (let c = item.column; c < item.column + item.span; c++)
        bottoms[c] = item.top + item.height + gap;
    }
    pending = [];
  }

  for (const item of items) {
    const span = item.width === 'full' ? columns : item.width === 'wide' ? Math.min(2, columns) : 1;
    let next = position(span);
    if (pending.length && next.top !== pending[0].top) {
      settle();
      next = position(span);
    }
    const placement = { ...next, span, height: Math.ceil(item.height) };
    placed.push(placement);
    pending.push(placement);
    for (let c = next.column; c < next.column + span; c++)
      bottoms[c] = next.top + placement.height + gap;
  }
  settle();
  // Fill small holes below a card without moving any cards or crossing one.
  // Different starting heights mean the common bottom need not be a step multiple.
  for (let i = 0; i < placed.length; i++) {
    const item = placed[i];
    let nextTop = Infinity;
    for (const next of placed.slice(i + 1)) {
      if (next.column < item.column + item.span && next.column + next.span > item.column)
        nextTop = Math.min(nextTop, next.top);
    }
    const extra = nextTop - gap - (item.top + item.height);
    if (extra > 0 && extra <= heightStep) item.height += extra;
  }
  return placed;
}

// CSS owns the responsive columns. Measure the inner content so a snapped card's
// extra space never becomes its intrinsic height on the next resize.
export function masonry(node: HTMLElement) {
  if (typeof ResizeObserver === 'undefined') return { destroy() {} };
  let frame = 0;
  let destroyed = false;
  const resize = new ResizeObserver(schedule);
  const mutations = new MutationObserver(() => {
    observe();
    schedule();
  });

  function schedule() {
    if (!frame && !destroyed) frame = requestAnimationFrame(layout);
  }

  function observe() {
    resize.disconnect();
    resize.observe(node);
    for (const content of node.querySelectorAll('[data-masonry-content]')) resize.observe(content);
  }

  function layout() {
    frame = 0;
    const cards = [...node.children].filter(
      (child): child is HTMLElement => child instanceof HTMLElement,
    );
    // Remove the old column placement before resolving the narrower grid. An
    // old span must not create implicit columns after the container shrinks.
    for (const card of cards) {
      card.style.gridColumnStart = '';
      card.style.gridColumnEnd = '';
      card.style.gridRowStart = '';
      card.style.gridRowEnd = '';
    }
    const style = getComputedStyle(node);
    const tracks = style.gridTemplateColumns.trim().split(/\s+/);
    if (!tracks.length || tracks.some((track) => !/^\d+(\.\d+)?px$/.test(track))) return;
    const gap = Math.round(parseFloat(style.columnGap) || 0);
    const items = cards.map((card): MasonryItem => {
      const content = card.querySelector<HTMLElement>('[data-masonry-content]');
      const box = getComputedStyle(card);
      const inset = ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth'].reduce(
        (sum, property) =>
          sum + (parseFloat(box[property as keyof CSSStyleDeclaration] as string) || 0),
        0,
      );
      const width = card.dataset.width;
      return {
        height:
          (content?.getBoundingClientRect().height ?? card.getBoundingClientRect().height) + inset,
        width: width === 'wide' || width === 'full' ? width : 'auto',
      };
    });
    const placed = layoutMasonry(items, tracks.length, gap);
    node.dataset.masonry = 'true';
    for (let i = 0; i < cards.length; i++) {
      const card = cards[i];
      const item = placed[i];
      card.style.gridColumnStart = String(item.column + 1);
      card.style.gridColumnEnd = `span ${item.span}`;
      card.style.gridRowStart = String(item.top + 1);
      card.style.gridRowEnd = `span ${item.height + gap}`;
      card.style.minHeight = `${item.height}px`;
    }
  }

  observe();
  mutations.observe(node, { childList: true });
  schedule();
  return {
    destroy() {
      destroyed = true;
      cancelAnimationFrame(frame);
      resize.disconnect();
      mutations.disconnect();
    },
  };
}
