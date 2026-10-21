<script lang="ts">
  import { flushSync } from 'svelte';
  import { base } from '$app/paths';
  import { version } from 'pdfjs-dist/package.json';
  import type { PDFDocumentProxy, PDFDocumentLoadingTask, RenderTask } from 'pdfjs-dist';
  import { ZoomIn, ZoomOut } from '@lucide/svelte';
  import { Button, IconButton } from '@minot/ui';
  import {
    DOUBLE_TAP_MS,
    DOUBLE_TAP_SCALE,
    DOUBLE_TAP_SLOP,
    KEY_ZOOM_STEP,
    MAX_SCALE,
    MIN_SCALE,
    TAP_SLOP,
    WHEEL_IDLE_MS,
    clampScale,
    distance,
    midpoint,
    normalizeWheelDelta,
    previewTransform,
    scrollAfterZoom,
    wheelScaleFactor,
    type Point,
  } from '$lib/imageZoom';

  let {
    url,
    onLoadError,
  }: {
    url: string;
    /** Called when the document cannot load; `status` is the HTTP status when the fetch failed. */
    onLoadError?: (status: number | null) => void;
  } = $props();
  let document = $state.raw<PDFDocumentProxy | null>(null);
  let pageNumber = $state(1);
  let error = $state('');
  let rendering = $state(false);
  let surface = $state<HTMLDivElement>();
  let scroller = $state<HTMLDivElement>();
  let width = $state(0);
  let controlsHeight = $state(0);
  /** The page's size over its fit width, kept across pages and reset per document. */
  let zoom = $state(1);

  /**
   * The canvas on screen and the page it holds. A zoom keeps it, scaled by
   * css, until the sharper one is drawn, so the page never blanks mid-zoom.
   */
  let shown: HTMLCanvasElement | null = null;
  let shownPage = 0;
  /** Whether `shown` holds a finished draw; until then a rerun starts it over. */
  let shownDrawn = false;

  $effect(() => {
    const requested = url;
    let active = true;
    let loading: PDFDocumentLoadingTask | undefined;
    document = null;
    error = '';
    pageNumber = 1;
    zoom = 1;
    endGestures();
    // The last document's page must not stand in for this one's.
    shown?.remove();
    shown = null;
    shownPage = 0;
    shownDrawn = false;
    async function load() {
      try {
        // PDF.js 6 no longer generates/evaluates JS for fonts/functions. Only
        // its canvas API is loaded: no scripting, annotation or XFA layer.
        const [pdf, worker] = await Promise.all([
          import('pdfjs-dist'),
          import('pdfjs-dist/build/pdf.worker.min.mjs?url'),
        ]);
        if (!active) return;
        pdf.GlobalWorkerOptions.workerSrc = worker.default;
        loading = pdf.getDocument({
          url: requested,
          wasmUrl: `${base}/pdfjs/${version}/wasm/`,
          cMapUrl: `${base}/pdfjs/${version}/cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${base}/pdfjs/${version}/standard_fonts/`,
          stopAtErrors: true,
          enableXfa: false,
          useWorkerFetch: false,
          useWasm: false,
          disableAutoFetch: true,
          disableStream: true,
          maxImageSize: 8_000_000,
          canvasMaxAreaInBytes: 32_000_000,
        });
        const loaded = await loading.promise;
        if (active) document = loaded;
      } catch (e) {
        if (!active) return;
        const name = e instanceof Error ? e.name : '';
        // pdf.js raises a ResponseException carrying the status for a failed fetch.
        const status =
          name === 'ResponseException' && typeof (e as { status?: unknown }).status === 'number'
            ? (e as { status: number }).status
            : null;
        onLoadError?.(status);
        error =
          name === 'PasswordException'
            ? 'This PDF needs a password. Download it to open in another app.'
            : name === 'InvalidPDFException'
              ? 'This PDF is invalid. Try downloading the original file.'
              : "Couldn't load this PDF. Try downloading the original file.";
      }
    }
    void load();
    return () => {
      active = false;
      void loading?.destroy().catch(() => {});
    };
  });

  $effect(() => {
    const pdf = document;
    const pageIndex = pageNumber;
    const host = surface;
    const display = Math.max(1, width || 800) * zoom;
    if (!pdf || !host) return;
    let active = true;
    let task: RenderTask | undefined;
    // A fresh canvas per render prevents a cancelled job and its replacement
    // from sharing a backing store while cancellation finishes asynchronously.
    const canvas = window.document.createElement('canvas');
    canvas.setAttribute('aria-label', `PDF page ${pageIndex}`);
    canvas.setAttribute('role', 'img');
    canvas.style.width = `${display}px`;
    const rezoom = shown !== null && shownDrawn && shown.isConnected && shownPage === pageIndex;
    if (rezoom) {
      shown!.style.width = `${display}px`;
    } else {
      host.replaceChildren(canvas);
      shown = canvas;
      shownPage = pageIndex;
      shownDrawn = false;
      rendering = true;
    }
    async function draw() {
      let page;
      try {
        page = await pdf!.getPage(pageIndex);
        if (!active) return;
        const original = page.getViewport({ scale: 1 });
        if (!(
          Number.isFinite(original.width) &&
          Number.isFinite(original.height) &&
          original.width > 0 &&
          original.height > 0
        ))
          throw new Error('Invalid page size');
        // A deep zoom past the caps is drawn at the cap and scaled up by css:
        // softer, but bounded in memory on a phone.
        const scale = Math.min(
          (display / original.width) * Math.min(window.devicePixelRatio || 1, 2),
          4096 / original.width,
          4096 / original.height,
          Math.sqrt(8_000_000 / (original.width * original.height)),
        );
        const viewport = page.getViewport({ scale });
        canvas.width = Math.max(1, Math.floor(viewport.width));
        canvas.height = Math.max(1, Math.floor(viewport.height));
        task = page.render({ canvas, viewport, annotationMode: 0 });
        await task.promise;
        if (active && !rezoom) shownDrawn = true;
        if (active && rezoom) {
          canvas.style.transform = shown!.style.transform;
          canvas.style.transformOrigin = shown!.style.transformOrigin;
          host!.replaceChildren(canvas);
          shown = canvas;
        }
      } catch {
        if (active) error = "Couldn't render this PDF page. Try downloading the original file.";
      } finally {
        // The page can be reused by a rapid Previous/Next. cleanup() refuses
        // while another render still owns it; document.destroy handles close.
        page?.cleanup();
        if (active && !rezoom) rendering = false;
      }
    }
    void draw();
    return () => {
      active = false;
      task?.cancel();
      if (canvas !== shown) canvas.remove();
    };
  });

  /**
   * A zoom in progress: what was measured when it began.
   *
   * The page is previewed by a css transform until the gesture ends, then laid
   * out at its new size, drawn again, and scrolled so the same content stays
   * under the fingers. Re-laying out and re-drawing every frame is too slow.
   */
  interface Zoom {
    zoom: number;
    scroll: Point;
    /** The page's top-left in the scrolled content. */
    offset: Point;
    /** Where the gesture began, in the scroller's visible box. */
    anchor: Point;
  }

  function local(x: number, y: number): Point {
    const box = scroller!.getBoundingClientRect();
    return { x: x - box.left, y: y - box.top };
  }

  function pageOffset(): Point {
    const box = scroller!.getBoundingClientRect();
    const page = (shown ?? surface!).getBoundingClientRect();
    return {
      x: page.left - box.left + scroller!.scrollLeft,
      y: page.top - box.top + scroller!.scrollTop,
    };
  }

  function begin(anchor: Point): Zoom | null {
    if (!scroller || !surface) return null;
    return {
      zoom,
      scroll: { x: scroller.scrollLeft, y: scroller.scrollTop },
      offset: pageOffset(),
      anchor,
    };
  }

  /** The factor actually left once the zoom range has had its say. */
  function factorFor(start: Zoom, factor: number): number {
    return clampScale(start.zoom * factor) / start.zoom;
  }

  function preview(start: Zoom, factor: number, anchor: Point) {
    if (!shown) return;
    const toPage = (p: Point) => ({
      x: p.x + start.scroll.x - start.offset.x,
      y: p.y + start.scroll.y - start.offset.y,
    });
    const t = previewTransform(toPage(start.anchor), toPage(anchor), factorFor(start, factor));
    shown.style.transformOrigin = '0 0';
    shown.style.transform = `translate(${t.x}px, ${t.y}px) scale(${t.scale})`;
  }

  function commit(start: Zoom, factor: number, anchor: Point) {
    if (!scroller) return;
    // Set from the clamp itself, so six steps of 1.4 land on the cap exactly.
    zoom = clampScale(start.zoom * factor);
    const applied = zoom / start.zoom;
    // Lay out at the new size now, so the scroll below lands in the same frame
    // as the preview comes off.
    flushSync();
    if (shown) shown.style.transform = '';
    const next = scrollAfterZoom({
      scroll: start.scroll,
      offsetBefore: start.offset,
      offsetAfter: pageOffset(),
      anchorStart: start.anchor,
      anchorNow: anchor,
      factor: applied,
    });
    scroller.scrollLeft = next.x;
    scroller.scrollTop = next.y;
  }

  /** Zoom to `target` around a point that stays put. */
  function zoomAt(target: number, anchor: Point) {
    const start = begin(anchor);
    if (start) commit(start, target / start.zoom, anchor);
  }

  /** The keyboard and the zoom buttons grow from the middle of what is visible. */
  function zoomFromCenter(factor: number) {
    if (!scroller) return;
    const box = scroller.getBoundingClientRect();
    zoomAt(zoom * factor, { x: box.width / 2, y: box.height / 2 });
  }

  function onKeydown(e: KeyboardEvent) {
    // Ctrl/Cmd with these keys is the browser's own zoom.
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === '+' || e.key === '=') zoomFromCenter(KEY_ZOOM_STEP);
    else if (e.key === '-' || e.key === '_') zoomFromCenter(1 / KEY_ZOOM_STEP);
    else if (e.key === '0') zoomFromCenter(MIN_SCALE / zoom);
    else return;
    e.preventDefault();
  }

  /*
   * Taps. Native scrolling pans the page, so the pointer events only have to
   * tell a tap from everything else: travel, a second finger, or a
   * `pointercancel` because the browser took the touch for a scroll.
   */
  const pressed = new Map<number, Point>();
  let tapStart: Point | null = null;
  let tapSpoiled = false;
  let lastTap: { at: number; point: Point } | null = null;

  function onPointerDown(e: PointerEvent) {
    if (e.button !== 0) return;
    if (pressed.size === 0) {
      tapStart = { x: e.clientX, y: e.clientY };
      // A press on the scroller's own scrollbar or padding is not a tap on the page.
      tapSpoiled = !(e.target instanceof Node && surface?.contains(e.target));
    } else {
      tapSpoiled = true;
    }
    pressed.set(e.pointerId, { x: e.clientX, y: e.clientY });
  }

  function onPointerMove(e: PointerEvent) {
    if (!pressed.has(e.pointerId) || !tapStart) return;
    if (distance(tapStart, { x: e.clientX, y: e.clientY }) > TAP_SLOP) tapSpoiled = true;
  }

  function onPointerUp(e: PointerEvent) {
    if (e.type === 'pointerup' && e.button !== 0) return;
    if (!pressed.delete(e.pointerId)) return;
    if (e.type === 'pointercancel') tapSpoiled = true;
    if (pressed.size > 0 || tapSpoiled) return;
    handleTap({ x: e.clientX, y: e.clientY });
  }

  /** A double tap toggles between fit and a reading zoom, around the tap. */
  function handleTap(point: Point) {
    const now = Date.now();
    const second =
      lastTap !== null &&
      now - lastTap.at <= DOUBLE_TAP_MS &&
      distance(lastTap.point, point) <= DOUBLE_TAP_SLOP;
    if (!second) {
      lastTap = { at: now, point };
      return;
    }
    lastTap = null;
    if (!scroller) return;
    zoomAt(zoom > MIN_SCALE ? MIN_SCALE : DOUBLE_TAP_SCALE, local(point.x, point.y));
  }

  /*
   * Pinch. Touch events rather than pointer events, because the scroller
   * keeps `touch-action: pan-x pan-y` for native one-finger panning, and a
   * browser that takes a touch for a scroll cancels its pointer. Preventing
   * the two-finger `touchmove` is what keeps the browser off the pinch.
   */
  let pinch: { start: Zoom; spread: number; factor: number; anchor: Point } | null = null;

  function touchPoints(e: TouchEvent): Point[] {
    return Array.from(e.touches, (t) => local(t.clientX, t.clientY));
  }

  function onTouchStart(e: TouchEvent) {
    const points = touchPoints(e);
    if (points.length < 2) return;
    tapSpoiled = true;
    if (pinch) commit(pinch.start, pinch.factor, pinch.anchor);
    endWheel();
    const anchor = midpoint(points[0], points[1]);
    const start = begin(anchor);
    if (!start) return;
    pinch = { start, spread: distance(points[0], points[1]), factor: 1, anchor };
  }

  function onTouchMove(e: TouchEvent) {
    if (!pinch || e.touches.length < 2) return;
    e.preventDefault();
    const points = touchPoints(e);
    pinch.factor = pinch.spread > 0 ? distance(points[0], points[1]) / pinch.spread : 1;
    pinch.anchor = midpoint(points[0], points[1]);
    preview(pinch.start, pinch.factor, pinch.anchor);
  }

  function onTouchEnd(e: TouchEvent) {
    if (!pinch || e.touches.length >= 2) return;
    const { start, factor, anchor } = pinch;
    pinch = null;
    commit(start, factor, anchor);
  }

  /**
   * A trackpad pinch, or Ctrl+wheel, as one burst with an idle end. A plain
   * wheel is left to scroll the page.
   */
  let wheel: { start: Zoom; factor: number; anchor: Point } | null = null;
  let wheelTimer: ReturnType<typeof setTimeout> | null = null;

  function onWheel(e: WheelEvent) {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    if (pinch || !scroller) return;
    const at = local(e.clientX, e.clientY);
    if (!wheel) {
      const start = begin(at);
      if (!start) return;
      wheel = { start, factor: 1, anchor: at };
    }
    wheel.factor *= wheelScaleFactor(
      normalizeWheelDelta(e.deltaY, e.deltaMode, scroller.clientHeight),
    );
    wheel.anchor = at;
    preview(wheel.start, wheel.factor, at);
    if (wheelTimer !== null) clearTimeout(wheelTimer);
    wheelTimer = setTimeout(finishWheel, WHEEL_IDLE_MS);
  }

  function finishWheel() {
    wheelTimer = null;
    if (!wheel) return;
    const { start, factor, anchor } = wheel;
    wheel = null;
    commit(start, factor, anchor);
  }

  function endWheel() {
    if (wheelTimer !== null) clearTimeout(wheelTimer);
    wheelTimer = null;
    wheel = null;
  }

  function endGestures() {
    endWheel();
    pinch = null;
    pressed.clear();
    lastTap = null;
    if (shown) shown.style.transform = '';
  }

  // Non-passive, so the pinch and Ctrl+wheel can keep the browser off them.
  $effect(() => {
    const el = scroller;
    if (!el) return;
    el.addEventListener('touchstart', onTouchStart, { passive: true });
    el.addEventListener('touchmove', onTouchMove, { passive: false });
    el.addEventListener('touchend', onTouchEnd);
    el.addEventListener('touchcancel', onTouchEnd);
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      el.removeEventListener('touchstart', onTouchStart);
      el.removeEventListener('touchmove', onTouchMove);
      el.removeEventListener('touchend', onTouchEnd);
      el.removeEventListener('touchcancel', onTouchEnd);
      el.removeEventListener('wheel', onWheel);
      endGestures();
    };
  });

  /** A new page starts at its top, wherever the last one was scrolled to. */
  function goToPage(next: number) {
    pageNumber = next;
    if (scroller) scroller.scrollTop = 0;
  }
</script>

<div class="pdf-preview" style:--pdf-controls-height={`${controlsHeight}px`}>
  {#if error}
    <p role="alert">{error}</p>
  {:else}
    <!-- A scrollable region has to be reachable from the keyboard, and the zoom
         keys are only taken while it has focus. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
    <div
      class="pdf-scroll"
      role="region"
      aria-label="PDF page"
      tabindex="0"
      bind:this={scroller}
      onkeydown={onKeydown}
      onpointerdown={onPointerDown}
      onpointermove={onPointerMove}
      onpointerup={onPointerUp}
      onpointercancel={onPointerUp}
    >
      {#if !document || rendering}<p class="caption" role="status">Loading PDF…</p>{/if}
      <div class="pdf-page" bind:this={surface} bind:clientWidth={width}></div>
    </div>
    {#if document}
      <nav class="pdf-controls" aria-label="PDF pages" bind:clientHeight={controlsHeight}>
        <Button
          variant="secondary"
          ariaLabel="Previous page"
          disabled={pageNumber === 1}
          onclick={() => goToPage(pageNumber - 1)}>Previous</Button
        >
        <span aria-live="polite">Page {pageNumber} of {document.numPages}</span>
        <Button
          variant="secondary"
          ariaLabel="Next page"
          disabled={pageNumber === document.numPages}
          onclick={() => goToPage(pageNumber + 1)}>Next</Button
        >
        <span class="zoom-controls hstack">
          <IconButton
            label="Zoom out"
            icon={ZoomOut}
            size="sm"
            disabled={zoom <= MIN_SCALE}
            onclick={() => zoomFromCenter(1 / KEY_ZOOM_STEP)}
          />
          <IconButton
            label="Zoom in"
            icon={ZoomIn}
            size="sm"
            disabled={zoom >= MAX_SCALE}
            onclick={() => zoomFromCenter(KEY_ZOOM_STEP)}
          />
        </span>
      </nav>
    {/if}
  {/if}
</div>

<style>
  .pdf-preview {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .pdf-scroll {
    flex: 1;
    min-height: 0;
    overflow: auto;
    padding: var(--space-2) var(--space-3);
    padding-bottom: calc(var(--pdf-controls-height) + var(--space-4));
    /* One finger scrolls natively; the pinch is handled in script, so the
       browser must not zoom the page on it or on a double tap. */
    touch-action: pan-x pan-y;
  }
  .pdf-preview > p {
    padding: var(--space-2) var(--space-3);
  }
  .pdf-controls {
    position: absolute;
    bottom: var(--space-2);
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: var(--space-2);
    max-width: calc(100% - var(--space-4));
    padding: var(--space-1);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    background: var(--surface-card);
    box-shadow: var(--shadow-md);
    white-space: nowrap;
    font-size: var(--text-sm);
  }
  /* A phone has no room for them beside the page buttons, and pinches instead. */
  @media (max-width: 640px) {
    .zoom-controls {
      display: none;
    }
  }
  .pdf-page {
    width: 100%;
  }
  /* Laid out at the zoomed width by script; one wider than the pane scrolls. */
  .pdf-page :global(canvas) {
    display: block;
    height: auto;
    margin-inline: auto;
    user-select: none;
    -webkit-touch-callout: none;
  }
</style>
