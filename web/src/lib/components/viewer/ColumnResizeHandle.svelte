<script lang="ts">
  /*
    A drag handle on a column header's right edge. It knows nothing about the
    table: it reports widths and the caller applies them, so it can move to
    minot once a second table wants it (web/AGENTS.md). The header cell it sits
    in must be positioned.
  */
  let {
    label,
    width,
    min = 48,
    max = 1600,
    step = 16,
    onresize,
    onfit,
  }: {
    /** The column's name, for "Resize <label> column". */
    label: string;
    /** The column's current width in px. */
    width: number;
    min?: number;
    max?: number;
    /** How far one arrow key press moves the edge, in px. */
    step?: number;
    onresize: (width: number) => void;
    /** Double click: fit the column to its content. */
    onfit: () => void;
  } = $props();

  let drag: { pointer: number; x: number; width: number } | null = null;

  const clamp = (value: number) => Math.round(Math.min(max, Math.max(min, value)));

  function pointerdown(event: PointerEvent) {
    if (event.button !== 0) return;
    event.preventDefault();
    (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
    drag = { pointer: event.pointerId, x: event.clientX, width };
  }

  function pointermove(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.pointer) return;
    onresize(clamp(drag.width + event.clientX - drag.x));
  }

  function pointerup(event: PointerEvent) {
    if (drag?.pointer === event.pointerId) drag = null;
  }

  function keydown(event: KeyboardEvent) {
    const delta = event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : null;
    if (delta === null) return;
    event.preventDefault();
    onresize(clamp(width + delta));
  }
</script>

<!-- A focusable separator is a widget (WAI-ARIA window splitter): the arrow keys move it. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="column-resize"
  role="separator"
  aria-orientation="vertical"
  aria-label={`Resize ${label} column`}
  aria-valuenow={Math.round(width)}
  aria-valuemin={min}
  aria-valuemax={max}
  title="Drag to resize, double click to fit"
  tabindex="0"
  onpointerdown={pointerdown}
  onpointermove={pointermove}
  onpointerup={pointerup}
  onpointercancel={pointerup}
  onkeydown={keydown}
  ondblclick={onfit}
></div>

<style>
  .column-resize {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    width: var(--space-2);
    cursor: col-resize;
    touch-action: none;
    /* The header cell clips its overflow, so a ring drawn outside would be cut. */
    outline-offset: calc(-1 * var(--focus-ring-width));
  }
  /* The cell clips a `.touch-target` overlay too, so the element itself widens. */
  @media (pointer: coarse) {
    .column-resize {
      width: var(--touch-min);
    }
  }
  /* The visible edge: a rule along the cell's right side, stronger while it is
     the thing being pointed at or moved. */
  .column-resize::after {
    content: '';
    position: absolute;
    top: 25%;
    bottom: 25%;
    right: 0;
    border-right: 1px solid var(--border-subtle);
  }
  .column-resize:hover::after,
  .column-resize:focus-visible::after,
  .column-resize:active::after {
    top: 0;
    bottom: 0;
    border-right: 2px solid var(--border-hover);
  }
</style>
