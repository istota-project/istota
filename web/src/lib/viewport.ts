/**
 * Latches the device safe-area insets against soft-keyboard transitions.
 *
 * `env(safe-area-inset-*)` is live: when iOS raises the keyboard it pushes the
 * viewport up and reports every inset as 0, because in that moment nothing is
 * behind the status bar or the home indicator. That is defensible as a reading
 * of the *current* viewport, but it is useless as a layout input — the physical
 * bezel has not moved, and anything sized from the inset silently resizes when a
 * keyboard appears. `.app-nav` derives its top padding from `--safe-top`, so its
 * height became a function of whether a text field happened to be focused, and
 * the app was left inconsistent once the keyboard went away.
 *
 * So the insets are measured here and written to `:root` as fixed pixel values,
 * which override the `env()` defaults in app.css (inline style beats a
 * stylesheet). They are re-measured only when the soft keyboard is down — i.e.
 * never mid-keyboard. Rotation is handled, since the settle loop below outlives
 * the transition.
 *
 * The app.css fallbacks stay as they are, so with JS disabled, during SSR, and
 * before this runs, the plain `env()` behaviour still applies.
 *
 * Deliberate cost: while the keyboard is up, `--safe-bottom` keeps its
 * home-indicator value instead of collapsing to 0, so the composer holds ~34px
 * of padding above the keyboard that it does not strictly need. That is a small
 * static gap, traded for a layout that does not move under the user mid-typing.
 */
const EDGES = ['top', 'bottom', 'left', 'right'] as const;

/**
 * Second job: pin the app's height in an installed iOS web app.
 *
 * iOS 26 ships a WebKit regression (bug 297779) where, after the soft keyboard
 * is dismissed, `visualViewport.height` stays smaller than `window.innerHeight`
 * and `visualViewport.offsetTop` never returns to 0. `dvh` follows the visual
 * viewport, so `body { height: 100dvh }` is left short and the app no longer
 * reaches the bottom of the screen — the band. It is a platform bug (it hits
 * apple.com too) with no official workaround, reportedly fixed in iOS 26.1.
 *
 * `window.innerHeight` is the value that stays correct through the transition,
 * so in standalone mode the height is driven from that instead, published as
 * `--app-height` for app.css to consume.
 *
 * Confined to standalone deliberately. In a browser tab `innerHeight` is the
 * layout viewport and does not track a collapsing toolbar, which is the exact
 * thing `dvh` was adopted to fix — so a tab keeps `dvh` and only the installed
 * app, where the bug lives and there is no toolbar, takes the override.
 */

/**
 * How much of the layout viewport has to be missing before we call it a
 * keyboard. Comfortably above the largest browser chrome that can come and go,
 * comfortably below the shortest soft keyboard.
 */
const KEYBOARD_OCCLUSION_PX = 120;

/**
 * The transition is settled once the measurement stops moving, not once a fixed
 * timer expires. A single post-event timeout was the old approach and it lost
 * the race whenever iOS took longer than expected to restore the viewport —
 * which is exactly what typing does, since dismissing a keyboard with content
 * in the field also unwinds the caret-tracking scroll and the autocorrect bar.
 */
const SETTLE_INTERVAL_MS = 120;
const SETTLE_STABLE_TICKS = 3;
const SETTLE_MAX_MS = 3000;

function isStandalone(): boolean {
  return (
    window.matchMedia?.('(display-mode: standalone)').matches === true ||
    // iOS predates the display-mode query for home-screen apps.
    (navigator as unknown as { standalone?: boolean }).standalone === true
  );
}

function isTextEntryFocused(): boolean {
  const el = document.activeElement as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

/** Is something (in practice, only ever a keyboard) eating the layout viewport? */
function viewportIsOccluded(): boolean {
  const vv = window.visualViewport;
  if (!vv) return false;
  // Scale-corrected, so a pinch-zoomed page is not mistaken for a keyboard.
  return window.innerHeight - vv.height * vv.scale > KEYBOARD_OCCLUSION_PX;
}

/**
 * Two signals, because neither is trustworthy alone.
 *
 * Focus is *necessary*: with no text entry focused there is no soft keyboard,
 * whatever `visualViewport` claims — and under the iOS 26 bug it goes on
 * claiming the viewport is short long after the keyboard is gone, so trusting
 * occlusion alone would freeze the measurement permanently.
 *
 * Occlusion is what makes focus *sufficient*: iOS keeps focus on the field when
 * the keyboard is dismissed by swiping down over it, and a focus-only test then
 * treats that state as keyboard-up forever, never re-measuring — the band comes
 * back and never leaves. Nothing occluding means the keyboard is down, focus or
 * not.
 */
function keyboardIsUp(): boolean {
  if (!isTextEntryFocused()) return false;
  return window.visualViewport ? viewportIsOccluded() : true;
}

/**
 * Undo the scroll iOS applies to keep the caret above the keyboard.
 *
 * This is the half that only shows up once you actually type: an empty composer
 * is already on screen, so focusing it scrolls nothing and the offset is 0 at
 * dismissal. Type enough to move the caret (or grow the textarea) and iOS scrolls
 * the layout viewport up; under the same WebKit regression that offset is not
 * always unwound when the keyboard goes, and a body sized to the full viewport
 * is then displaced — the gap at the bottom, back again.
 *
 * Guarded on the document having nothing to scroll, which is this app's steady
 * state (`body` is a fixed-height flex column; the scrolling happens in panes
 * inside it). If there *is* scrollable overflow the offset is the user's own
 * scroll position and must not be touched.
 */
function releaseResidualScroll(): void {
  const el = document.scrollingElement ?? document.documentElement;
  if (!el) return;
  if (el.scrollTop === 0 && window.scrollY === 0) return;
  if (el.scrollHeight > el.clientHeight + 1) return;
  try {
    window.scrollTo(0, 0);
  } catch {
    el.scrollTop = 0;
  }
}

export function installViewportGuard(): () => void {
  if (typeof window === 'undefined') return () => {};

  // A 0x0 probe whose padding resolves the env() values, since they cannot be
  // read from a custom property directly.
  const probe = document.createElement('div');
  probe.setAttribute('aria-hidden', 'true');
  probe.style.cssText =
    'position:fixed;top:0;left:0;width:0;height:0;visibility:hidden;pointer-events:none;' +
    EDGES.map((e) => `padding-${e}:env(safe-area-inset-${e},0px)`).join(';');
  document.body.appendChild(probe);

  const root = document.documentElement;
  const standalone = isStandalone();

  let timer: ReturnType<typeof setTimeout> | undefined;
  let deadline = 0;
  let stableTicks = 0;
  let lastReading = '';
  const written = new Map<string, string>();

  function write(prop: string, value: string): void {
    if (written.get(prop) === value) return;
    written.set(prop, value);
    root.style.setProperty(prop, value);
  }

  /** One measurement pass. Returns a fingerprint of the reading, or null if the keyboard is up. */
  function measure(): string | null {
    if (keyboardIsUp()) return null;
    releaseResidualScroll();

    const parts: string[] = [];
    if (standalone) {
      const height = `${window.innerHeight}px`;
      write('--app-height', height);
      parts.push(height);
    }
    const cs = getComputedStyle(probe);
    for (const edge of EDGES) {
      const value = cs.getPropertyValue(`padding-${edge}`);
      // An all-zero reading mid-transition would latch the wrong thing, which is
      // what the stability requirement below is for: a value still in motion
      // never accumulates enough identical ticks to end the session, so the last
      // thing written is a settled one.
      if (!value) continue;
      write(`--safe-${edge}`, value);
      parts.push(value);
    }
    return parts.join('|');
  }

  function tick(): void {
    timer = undefined;
    const reading = measure();
    // Keyboard up: nothing to measure, and no point burning ticks until it goes.
    // Its dismissal fires `focusout` / a visualViewport resize, which restarts us.
    if (reading === null) return;

    if (reading === lastReading) stableTicks += 1;
    else {
      lastReading = reading;
      stableTicks = 1;
    }
    if (stableTicks >= SETTLE_STABLE_TICKS) return;
    if (Date.now() >= deadline) return;
    timer = setTimeout(tick, SETTLE_INTERVAL_MS);
  }

  /**
   * (Re)start a settle session. Cheap to call from every event that could mean
   * the viewport moved — a session in progress is simply extended.
   */
  function settle(): void {
    clearTimeout(timer);
    deadline = Date.now() + SETTLE_MAX_MS;
    stableTicks = 0;
    lastReading = '';
    tick();
  }

  const onVisibilityChange = () => {
    if (document.visibilityState === 'visible') settle();
  };

  settle();
  const vv = window.visualViewport;
  window.addEventListener('focusout', settle);
  window.addEventListener('orientationchange', settle);
  window.addEventListener('resize', settle);
  window.addEventListener('pageshow', settle);
  document.addEventListener('visibilitychange', onVisibilityChange);
  vv?.addEventListener('resize', settle);
  // The keyboard pans the visual viewport as well as resizing it, and on the way
  // out the pan is sometimes the only event that fires.
  vv?.addEventListener('scroll', settle);

  return () => {
    clearTimeout(timer);
    window.removeEventListener('focusout', settle);
    window.removeEventListener('orientationchange', settle);
    window.removeEventListener('resize', settle);
    window.removeEventListener('pageshow', settle);
    document.removeEventListener('visibilitychange', onVisibilityChange);
    vv?.removeEventListener('resize', settle);
    vv?.removeEventListener('scroll', settle);
    probe.remove();
    root.style.removeProperty('--app-height');
    for (const edge of EDGES) root.style.removeProperty(`--safe-${edge}`);
  };
}
