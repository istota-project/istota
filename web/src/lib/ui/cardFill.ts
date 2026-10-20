export interface FillItem {
  width: 'auto' | 'wide' | 'full';
  fill: boolean;
}

/**
 * The span each card takes once `fill` cards have widened into the empty
 * tracks after them. It replays the grid's own sparse, row-major
 * auto-placement, so the result is what the browser would lay out. A fill
 * card grows only when it is the last card in its row: the next card has
 * already wrapped, so widening into the gap moves nothing else.
 */
export function fillSpans(items: FillItem[], columns: number): number[] {
  const spans = items.map((item) =>
    item.width === 'full' ? columns : item.width === 'wide' ? Math.min(2, columns) : 1,
  );
  const starts: number[] = [];
  let column = 0;
  for (const span of spans) {
    if (column + span > columns) column = 0;
    starts.push(column);
    column += span;
  }
  for (let i = 0; i < items.length; i++) {
    if (!items[i].fill) continue;
    const end = starts[i] + spans[i];
    const nextWrapped = i + 1 >= items.length || starts[i + 1] < end;
    if (nextWrapped && end < columns) spans[i] = columns - starts[i];
  }
  return spans;
}

// CSS owns the column count; this reads it back and writes a span only on the
// fill cards it widens, so the grid's own `data-width` rules keep the rest.
export function cardFill(node: HTMLElement) {
  if (typeof ResizeObserver === 'undefined') return { destroy() {} };
  let frame = 0;
  const resize = new ResizeObserver(schedule);
  const mutations = new MutationObserver(schedule);

  function schedule() {
    if (!frame) frame = requestAnimationFrame(layout);
  }

  function layout() {
    frame = 0;
    const cards = [...node.children].filter(
      (child): child is HTMLElement => child instanceof HTMLElement,
    );
    // Clear the old spans first: one left over from a wider grid adds implicit
    // tracks, and the column count read below would include them.
    for (const card of cards) card.style.gridColumn = '';
    const tracks = getComputedStyle(node).gridTemplateColumns.trim().split(/\s+/).length;
    const items = cards.map((card): FillItem => {
      const declared = card.dataset.width;
      return {
        width: declared === 'wide' || declared === 'full' ? declared : 'auto',
        fill: card.dataset.fill !== undefined,
      };
    });
    const spans = fillSpans(items, tracks);
    cards.forEach((card, i) => {
      card.style.gridColumn = items[i].fill && spans[i] > 1 ? `span ${spans[i]}` : '';
    });
  }

  resize.observe(node);
  mutations.observe(node, { childList: true });
  schedule();
  return {
    destroy() {
      cancelAnimationFrame(frame);
      resize.disconnect();
      mutations.disconnect();
    },
  };
}
