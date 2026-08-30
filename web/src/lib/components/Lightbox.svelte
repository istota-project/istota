<script lang="ts">
  import { onMount } from 'svelte';
  import { ChevronLeft, ChevronRight } from 'lucide-svelte';
  import {
    DOUBLE_TAP_MS,
    DOUBLE_TAP_SCALE,
    DOUBLE_TAP_SLOP,
    FIT,
    KEY_ZOOM_STEP,
    TAP_SLOP,
    WHEEL_IDLE_MS,
    applyGesture,
    distance,
    isZoomed,
    midpoint,
    normalizeWheelDelta,
    wheelScaleFactor,
    type Geometry,
    type Point,
    type ZoomState,
  } from '$lib/imageZoom';

  let {
    images = [],
    index = null,
    onClose,
  }: {
    images?: string[];
    index?: number | null;
    onClose: () => void;
  } = $props();

  let current = $state<number | null>(null);
  let zoom = $state<ZoomState>(FIT);
  let smooth = $state(false);
  let imgEl = $state<HTMLImageElement | null>(null);
  let backdropEl = $state<HTMLDivElement | null>(null);

  $effect(() => {
    current = index;
    resetGestures();
  });

  /**
   * The live pointers, and the state the current gesture started from.
   *
   * Plain variables rather than `$state`: nothing renders from them, and a
   * pinch writes them on every pointermove.
   */
  const pointers = new Map<number, Point>();
  let gestureStart: { state: ZoomState; anchor: Point; spread: number } | null = null;
  /** Has this gesture stopped being a tap — by travelling, or by multi-touch? */
  let dragged = false;
  /** Where the gesture began, which is what decides what a tap ending it means. */
  let tapTarget: 'image' | 'backdrop' = 'backdrop';
  let lastTap: { at: number; point: Point } | null = null;
  let closeTimer: ReturnType<typeof setTimeout> | null = null;
  let wheelGesture: { state: ZoomState; anchor: Point; factor: number } | null = null;
  let wheelIdleTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Drop every scrap of gesture state and go back to the fit scale.
   *
   * Everything here has to be cleared together, and this is the only place that
   * does it, because the component is **never unmounted**: the one caller
   * (`routes/feeds/+page.svelte`) renders `<Lightbox>` unconditionally and the
   * `{#if}` that hides it is inside. So the teardown in `onMount` does not run
   * between an open and the next one, and anything left behind here outlives
   * the image it belonged to — a pending close fires over the next image, a
   * `lastTap` pairs with a tap on a different one, and a pointer id that never
   * got its `pointerup` makes every later tap look like half a pinch.
   */
  function resetGestures() {
    zoom = FIT;
    smooth = false;
    cancelPendingClose();
    endWheelGesture();
    pointers.clear();
    gestureStart = null;
    dragged = false;
    lastTap = null;
  }

  function cancelPendingClose() {
    if (closeTimer === null) return;
    clearTimeout(closeTimer);
    closeTimer = null;
  }

  function endWheelGesture() {
    wheelGesture = null;
    if (wheelIdleTimer === null) return;
    clearTimeout(wheelIdleTimer);
    wheelIdleTimer = null;
  }

  function next(e?: Event) {
    e?.stopPropagation();
    if (current === null || images.length === 0) return;
    current = (current + 1) % images.length;
    resetGestures();
  }

  function prev(e?: Event) {
    e?.stopPropagation();
    if (current === null || images.length === 0) return;
    current = (current - 1 + images.length) % images.length;
    resetGestures();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (current === null) return;
    if (e.key === 'Escape') onClose();
    else if (e.key === 'ArrowRight') next();
    else if (e.key === 'ArrowLeft') prev();
    else if (e.key === '+' || e.key === '=') zoomFromCenter(KEY_ZOOM_STEP);
    else if (e.key === '-' || e.key === '_') zoomFromCenter(1 / KEY_ZOOM_STEP);
    else if (e.key === '0') resetGestures();
    else return;
    // Only the zoom keys are ours alone. Escape and the arrows are also bound
    // by the feed reader underneath, on `document`, and it registers first — so
    // claiming them here would suppress the browser's default without stopping
    // the other handler, which is a promise this cannot keep.
    if (e.key !== 'Escape' && e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') {
      e.preventDefault();
    }
  }

  /**
   * Zoom without a pointer to anchor on.
   *
   * The keyboard is the only way in for anyone who cannot pinch or hold a
   * mouse, and it is also what the `+` / `-` / `0` keys do in every image
   * viewer. The centre of the overlay stands in for the anchor, which is where
   * a gesture-less zoom is expected to grow from.
   */
  function zoomFromCenter(factor: number) {
    const geom = geometry();
    if (!geom) return;
    smooth = true;
    zoom = applyGesture(
      zoom,
      { scaleFactor: factor, anchorStart: geom.origin, anchorNow: geom.origin },
      geom,
    );
  }

  onMount(() => {
    document.addEventListener('keydown', handleKeydown);
    return () => {
      document.removeEventListener('keydown', handleKeydown);
      cancelPendingClose();
      endWheelGesture();
    };
  });

  /**
   * What the transform arithmetic needs, measured off the live elements.
   *
   * Nothing here reads a transformed box. `offsetWidth`/`offsetHeight` are
   * layout sizes, so they are the fit size directly rather than a rect that has
   * to be divided by the current scale — which also means a measurement taken
   * while a `smooth` transition is still running is not the interpolated one.
   *
   * The backdrop is the box the image is centred in (`position: fixed`, inset
   * 0, flex-centred), so it gives both the origin and the viewport, and gives
   * them consistently. `window.innerHeight` would be neither: it reports the
   * *visual* viewport, which on iOS diverges from the layout viewport the image
   * is actually laid out in — the divergence this app already carries
   * `--app-height` to work around.
   */
  function geometry(): Geometry | null {
    if (!imgEl || !backdropEl) return null;
    const box = backdropEl.getBoundingClientRect();
    return {
      origin: { x: box.left + box.width / 2, y: box.top + box.height / 2 },
      fit: { width: imgEl.offsetWidth, height: imgEl.offsetHeight },
      viewport: { width: box.width, height: box.height },
    };
  }

  /** Restart the gesture from wherever the fingers are now. */
  function beginGesture() {
    const points = [...pointers.values()];
    if (points.length === 0) {
      gestureStart = null;
      return;
    }
    gestureStart =
      points.length >= 2
        ? {
            state: zoom,
            anchor: midpoint(points[0], points[1]),
            spread: distance(points[0], points[1]),
          }
        : { state: zoom, anchor: points[0], spread: 0 };
  }

  /** The nav buttons keep their own behaviour; a tap on one is not a gesture. */
  function isControl(target: EventTarget | null): boolean {
    return target instanceof Element && target.closest('.controls') !== null;
  }

  function onPointerDown(e: PointerEvent) {
    if (isControl(e.target)) return;
    cancelPendingClose();
    endWheelGesture();
    smooth = false;
    backdropEl?.setPointerCapture?.(e.pointerId);
    if (pointers.size === 0) {
      dragged = false;
      tapTarget = e.target === imgEl ? 'image' : 'backdrop';
    } else {
      // A second finger means a pinch, and a pinch is never a tap — even one
      // that lands and lifts without moving.
      dragged = true;
    }
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    beginGesture();
  }

  function onPointerMove(e: PointerEvent) {
    if (!pointers.has(e.pointerId) || !gestureStart) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    const geom = geometry();
    if (!geom) return;
    const points = [...pointers.values()];

    if (points.length >= 2) {
      // A pinch is a scale and a two-finger pan at once: the midpoint carries
      // the pan, the spread carries the scale.
      const spread = distance(points[0], points[1]);
      dragged = true;
      zoom = applyGesture(
        gestureStart.state,
        {
          scaleFactor: gestureStart.spread > 0 ? spread / gestureStart.spread : 1,
          anchorStart: gestureStart.anchor,
          anchorNow: midpoint(points[0], points[1]),
        },
        geom,
      );
      e.preventDefault();
      return;
    }

    // Travel ends the tap whatever the scale. Only the panning is conditional:
    // at the fit scale there is nowhere to pan to, but a swipe is still a
    // swipe and must not be delivered as a tap — which at the fit scale is
    // what closes the overlay.
    if (distance(gestureStart.anchor, points[0]) > TAP_SLOP) dragged = true;
    if (!isZoomed(gestureStart.state)) return;
    zoom = applyGesture(
      gestureStart.state,
      { scaleFactor: 1, anchorStart: gestureStart.anchor, anchorNow: points[0] },
      geom,
    );
    e.preventDefault();
  }

  function onPointerUp(e: PointerEvent) {
    const lifted = pointers.get(e.pointerId);
    pointers.delete(e.pointerId);
    if (backdropEl?.hasPointerCapture?.(e.pointerId)) backdropEl.releasePointerCapture(e.pointerId);

    if (pointers.size > 0) {
      // One finger of a pinch lifted; carry on panning with what is left.
      beginGesture();
      return;
    }
    gestureStart = null;
    if (e.type === 'pointercancel' || dragged || !lifted) return;
    handleTap(lifted);
  }

  /**
   * A tap, once it is known not to be a drag.
   *
   * A tap on the backdrop closes at once. A tap on the image waits out the
   * double-tap window first, because the two gestures are identical until the
   * second tap either arrives or does not — the backdrop carries no double-tap
   * meaning, so it needs no such wait.
   */
  function handleTap(point: Point) {
    if (tapTarget === 'backdrop') {
      onClose();
      return;
    }

    const now = Date.now();
    const paired =
      lastTap !== null &&
      now - lastTap.at < DOUBLE_TAP_MS &&
      distance(lastTap.point, point) < DOUBLE_TAP_SLOP;

    if (paired) {
      lastTap = null;
      toggleZoom(point);
      return;
    }

    lastTap = { at: now, point };
    if (isZoomed(zoom)) return;
    closeTimer = setTimeout(() => {
      closeTimer = null;
      onClose();
    }, DOUBLE_TAP_MS);
  }

  function toggleZoom(point: Point) {
    const geom = geometry();
    if (!geom) return;
    const target = isZoomed(zoom) ? 1 : DOUBLE_TAP_SCALE;
    smooth = true;
    zoom = applyGesture(
      zoom,
      { scaleFactor: target / zoom.scale, anchorStart: point, anchorNow: point },
      geom,
    );
  }

  /**
   * A trackpad pinch arrives here rather than as pointer events, with `ctrlKey`
   * set by the platform. A plain scroll is left alone: over a modal it means
   * the page behind, not this image.
   *
   * The burst is treated as one gesture, with the factors multiplied and
   * applied to the state it started from, because `applyGesture` must not be
   * fed its own output — every call re-clamps, so accumulating frame by frame
   * would not come back to where it started after a pinch that ran the image
   * into an edge. A wheel has no finger to lift, so the gesture ends on idle.
   */
  function onWheel(e: WheelEvent) {
    if (!e.ctrlKey && !e.metaKey) return;
    const geom = geometry();
    if (!geom) return;

    const at = { x: e.clientX, y: e.clientY };
    if (!wheelGesture) wheelGesture = { state: zoom, anchor: at, factor: 1 };
    wheelGesture.factor *= wheelScaleFactor(
      normalizeWheelDelta(e.deltaY, e.deltaMode, geom.viewport.height),
    );

    smooth = false;
    zoom = applyGesture(
      wheelGesture.state,
      { scaleFactor: wheelGesture.factor, anchorStart: wheelGesture.anchor, anchorNow: at },
      geom,
    );
    e.preventDefault();

    if (wheelIdleTimer !== null) clearTimeout(wheelIdleTimer);
    wheelIdleTimer = setTimeout(endWheelGesture, WHEEL_IDLE_MS);
  }
</script>

{#if current !== null && images.length > 0}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="lightbox open"
    bind:this={backdropEl}
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
    onwheel={onWheel}
  >
    <img
      bind:this={imgEl}
      src={images[current]}
      alt=""
      draggable="false"
      class:zoomed={isZoomed(zoom)}
      style:transform="translate({zoom.x}px, {zoom.y}px) scale({zoom.scale})"
      style:transition={smooth ? 'transform 180ms ease-out' : 'none'}
    />
    {#if images.length > 1}
      <div class="controls">
        <button class="nav" onclick={prev} aria-label="Previous image">
          <ChevronLeft size={24} />
        </button>
        <div class="counter">{current + 1} / {images.length}</div>
        <button class="nav" onclick={next} aria-label="Next image">
          <ChevronRight size={24} />
        </button>
      </div>
    {/if}
  </div>
{/if}

<style>
  .lightbox {
    position: fixed;
    inset: 0;
    z-index: var(--z-lightbox);
    /* design-lint-allow: fixed chrome — the lightbox is a theme-invariant dark
       overlay over the image; darkening is the whole point of the surface. */
    background: rgba(0, 0, 0, 0.9);
    display: flex;
    justify-content: center;
    align-items: center;
    cursor: zoom-out;
    /* Clip the zoomed image to the overlay, so it cannot paint over the device
       insets or the control bar below it. */
    overflow: hidden;
    /* The gesture is ours, so the browser must not claim it first: without
       this, a pinch scrolls and zooms the page instead. It goes on the
       backdrop rather than the image because a pinch routinely puts one finger
       on the letterboxing beside a fitted image, and that finger has to reach
       the same handler — a gesture the backdrop does not see is delivered as a
       one-finger swipe on the image. */
    touch-action: none;
  }
  .lightbox img {
    max-width: 90vw;
    /* Leave room for the bottom control bar so it never covers the image, and
		   for the device insets — 5vh of slack is under the Dynamic Island's height
		   on a phone, so without this the top of a tall image sits behind it. */
    max-height: calc(90dvh - 4rem - var(--safe-top) - var(--safe-bottom));
    object-fit: contain;
    /* A drag across an image is a native image-drag on a desktop browser and a
       text selection on the way out of one; a long press in a WKWebView raises
       the system callout sheet. Each of the three interrupts a pan. */
    user-select: none;
    -webkit-user-drag: none;
    -webkit-touch-callout: none;
  }
  .lightbox img.zoomed {
    cursor: grab;
  }
  /* Full-bleed overlay, so its controls carry their own safe-area offsets. */
  .controls {
    position: absolute;
    bottom: max(1rem, var(--safe-bottom));
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: default;
  }
  .nav {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 2.5rem;
    height: 2.5rem;
    border: none;
    border-radius: 50%;
    /* design-lint-allow-begin: fixed chrome — the lightbox is a theme-invariant
       dark overlay over the image, so its controls stay dark-on-white in both
       themes. */
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    /* design-lint-allow-end */
    cursor: pointer;
    transition: background 120ms;
  }
  .nav:hover {
    /* design-lint-allow: fixed chrome — see .nav above. */
    background: rgba(0, 0, 0, 0.75);
  }
  .counter {
    padding: var(--space-1) var(--space-2);
    /* design-lint-allow-begin: fixed chrome — see .nav above. */
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    /* design-lint-allow-end */
    font-size: 0.8rem;
    border-radius: var(--radius-sm);
    pointer-events: none;
    white-space: nowrap;
  }
</style>
