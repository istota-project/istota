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

  // A card with nothing below it yet. Its bottom is an edge a later row may snap
  // to, since grid rounding alone leaves cards that start at different tops
  // ending a few pixels apart.
  function isOpen(item: MasonryPlacement) {
    for (let c = item.column; c < item.column + item.span; c++)
      if (bottoms[c] !== item.top + item.height + gap) return false;
    return true;
  }

  function settle() {
    const edges = placed.filter((item) => !pending.includes(item) && isOpen(item));
    const top = pending[0]?.top ?? 0;
    const sorted = [...pending].sort((a, b) => a.height - b.height);
    for (let start = 0; start < sorted.length;) {
      let end = start + 1;
      while (end < sorted.length && sorted[end].height - sorted[start].height <= heightStep) end++;
      const need = top + sorted[end - 1].height;
      const above = edges
        .map((item) => item.top + item.height)
        .filter((edge) => edge >= need && edge - need <= heightStep);
      let bottom = top + Math.ceil(sorted[end - 1].height / heightStep) * heightStep;
      const below = edges
        .map((item) => item.top + item.height)
        .filter((edge) => edge < need && need - edge <= heightStep);
      if (above.length) bottom = Math.min(...above);
      else if (below.some((edge) => bottom - edge > heightStep)) bottom = need;
      for (const item of edges) {
        const edge = item.top + item.height;
        if (edge < bottom && bottom - edge <= heightStep) item.height = bottom - item.top;
      }
      for (let i = start; i < end; i++) sorted[i].height = bottom - top;
      start = end;
    }
    for (const item of edges) {
      for (let c = item.column; c < item.column + item.span; c++)
        bottoms[c] = item.top + item.height + gap;
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
// extra space never becomes its intrinsic height on the next resize. A card with
// no `[data-masonry-content]` is measured itself, with its snapped height cleared
// first; a shrink inside it moves no observed box, so DOM changes anywhere in the
// grid also schedule a layout.
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
    for (const card of node.children) {
      resize.observe(card.querySelector('[data-masonry-content]') ?? card);
    }
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
      card.style.minHeight = '';
    }
    const style = getComputedStyle(node);
    const tracks = style.gridTemplateColumns.trim().split(/\s+/);
    if (!tracks.length || tracks.some((track) => !/^\d+(\.\d+)?px$/.test(track))) return;
    const gap = Math.round(parseFloat(style.columnGap) || 0);
    const items = cards.map((card): MasonryItem => {
      const declared = card.dataset.width;
      const width = declared === 'wide' || declared === 'full' ? declared : 'auto';
      const content = card.querySelector<HTMLElement>('[data-masonry-content]');
      if (!content) {
        return { height: card.getBoundingClientRect().height, width };
      }
      const box = getComputedStyle(card);
      const inset = ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth'].reduce(
        (sum, property) =>
          sum + (parseFloat(box[property as keyof CSSStyleDeclaration] as string) || 0),
        0,
      );
      return { height: content.getBoundingClientRect().height + inset, width };
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
  mutations.observe(node, { childList: true, subtree: true, characterData: true });
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
