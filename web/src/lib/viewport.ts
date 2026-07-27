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
 * How far either viewport may sit below its keyboard-free baseline and still
 * count as restored. Well under the shortest soft keyboard, well over the
 * rounding and toolbar jitter a settled viewport shows.
 */
const RESTORED_TOLERANCE_PX = 48;

/**
 * The transition is settled once the measurement stops moving, not once a fixed
 * timer expires. A single post-event timeout was the old approach and it lost
 * the race whenever iOS took longer than expected to restore the viewport —
 * which is exactly what typing does, since dismissing a keyboard with content
 * in the field also unwinds the caret-tracking scroll and the autocorrect bar.
 *
 * The floor matters as much as the stability: `focusout` fires while the
 * keyboard is still animating away, and a reading can hold steady for a couple
 * of ticks mid-animation. Nothing is accepted as final before the floor has
 * passed, so a session always outlives the animation it was triggered by.
 */
const SETTLE_INTERVAL_MS = 120;
const SETTLE_STABLE_TICKS = 3;
const SETTLE_MIN_MS = 600;
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

/**
 * The last settled keyboard-free geometry, both viewports, recorded whenever a
 * measurement settles with no text entry focused. Reset on rotation, which
 * changes both legitimately.
 */
type Baseline = { inner: number; visual: number };

function currentGeometry(): Baseline {
  const vv = window.visualViewport;
  return {
    inner: window.innerHeight,
    // Scale-corrected, so a pinch-zoomed page is not mistaken for a keyboard.
    visual: vv ? vv.height * vv.scale : 0,
  };
}

/**
 * Focus decides, geometry can only overrule it in one direction.
 *
 * Focus is the reliable signal and stays the gate: no text entry focused, no
 * soft keyboard, whatever the viewport claims. That matters because under the
 * iOS 26 bug `visualViewport` goes on reporting the viewport short long after
 * the keyboard is gone, so anything that trusted it alone would freeze the
 * measurement permanently.
 *
 * The one case focus gets wrong is a keyboard dismissed by swiping down over
 * it, which iOS does without blurring the field: focus alone then reads as
 * keyboard-up forever and the app is never measured again. So a focused field
 * is overruled — but only when *both* viewports have returned to the geometry
 * last seen with no keyboard. Requiring both is what makes this safe on either
 * platform: an installed iOS app resizes the layout viewport for the keyboard
 * (so `innerHeight` drops) while a browser tab resizes only the visual one (so
 * `visualViewport.height` drops), and neither is universal. Testing just one of
 * them — occlusion, as this first shipped — reads "no keyboard" on the platform
 * whose *other* viewport moved, and then latches keyboard-shaped values as if
 * they were the settled ones. That turned an intermittent gap into a permanent
 * one.
 */
function keyboardIsUp(baseline: Baseline | null): boolean {
  if (!isTextEntryFocused()) return false;
  // No keyboard-free reading to compare against yet: assume the keyboard.
  if (!baseline) return true;
  const now = currentGeometry();
  const innerRestored = now.inner >= baseline.inner - RESTORED_TOLERANCE_PX;
  const visualRestored =
    !window.visualViewport ||
    baseline.visual === 0 ||
    now.visual >= baseline.visual - RESTORED_TOLERANCE_PX;
  return !(innerRestored && visualRestored);
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

/**
 * On-device readout, off unless asked for.
 *
 * Everything this module guards against is a platform behaviour that exists
 * only on a real phone — which viewport a keyboard resizes, whether an offset
 * is unwound — and none of it is observable from a desktop browser or a test.
 * Load any page with `?vpdebug=1` to pin the live numbers to the corner
 * (`?vpdebug=0` clears it); the setting survives navigation so it can be turned
 * on in Safari and read in the installed app.
 */
const DEBUG_KEY = 'istota_viewport_debug';

function debugEnabled(): boolean {
  try {
    const q = new URLSearchParams(window.location.search).get('vpdebug');
    if (q === '1') localStorage.setItem(DEBUG_KEY, '1');
    if (q === '0') localStorage.removeItem(DEBUG_KEY);
    return localStorage.getItem(DEBUG_KEY) === '1';
  } catch {
    return false;
  }
}

function makeDebugPanel(): HTMLElement {
  const el = document.createElement('pre');
  el.style.cssText =
    'position:fixed;top:0;right:0;z-index:9999;margin:0;padding:4px 6px;' +
    'font:10px/1.35 ui-monospace,monospace;white-space:pre;pointer-events:none;' +
    'background:rgb(0 0 0 / 0.72);color:#0f0;border-bottom-left-radius:6px;';
  document.body.appendChild(el);
  return el;
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
  let startedAt = 0;
  let stableTicks = 0;
  let lastReading = '';
  let baseline: Baseline | null = null;
  const written = new Map<string, string>();
  const debugPanel = debugEnabled() ? makeDebugPanel() : null;

  function paintDebug(state: string): void {
    if (!debugPanel) return;
    const vv = window.visualViewport;
    const el = document.scrollingElement ?? document.documentElement;
    debugPanel.textContent = [
      `${state}${standalone ? ' pwa' : ''}${isTextEntryFocused() ? ' focus' : ''}`,
      `inner ${window.innerHeight}  vv ${vv ? Math.round(vv.height * vv.scale) : '-'}`,
      `base  ${baseline ? `${baseline.inner}/${Math.round(baseline.visual)}` : '-'}`,
      `off   ${vv ? Math.round(vv.offsetTop) : '-'}  scroll ${Math.round(el?.scrollTop ?? 0)}`,
      `app   ${written.get('--app-height') ?? '-'}  sb ${written.get('--safe-bottom') ?? '-'}`,
    ].join('\n');
  }

  function write(prop: string, value: string): void {
    if (written.get(prop) === value) return;
    written.set(prop, value);
    root.style.setProperty(prop, value);
  }

  /** One measurement pass. Returns a fingerprint of the reading, or null if the keyboard is up. */
  function measure(): string | null {
    if (keyboardIsUp(baseline)) return null;
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
    if (reading === null) {
      paintDebug('held');
      return;
    }

    if (reading === lastReading) stableTicks += 1;
    else {
      lastReading = reading;
      stableTicks = 1;
    }

    const elapsed = Date.now() - startedAt;
    const settled = stableTicks >= SETTLE_STABLE_TICKS && elapsed >= SETTLE_MIN_MS;
    if (settled) {
      // The reference the focused-but-restored test compares against, taken only
      // from a settled reading with nothing focused — so a value still moving
      // through a keyboard transition can never become the baseline.
      if (!isTextEntryFocused()) baseline = currentGeometry();
      paintDebug('settled');
      return;
    }
    paintDebug('sampling');
    if (elapsed >= SETTLE_MAX_MS) return;
    timer = setTimeout(tick, SETTLE_INTERVAL_MS);
  }

  /**
   * (Re)start a settle session. Cheap to call from every event that could mean
   * the viewport moved — a session in progress is simply extended.
   */
  function settle(): void {
    clearTimeout(timer);
    startedAt = Date.now();
    stableTicks = 0;
    lastReading = '';
    tick();
  }

  const onVisibilityChange = () => {
    if (document.visibilityState === 'visible') settle();
  };

  // Rotation changes both viewports legitimately, so the old keyboard-free
  // reference is meaningless; drop it and let the session re-establish one.
  const onOrientationChange = () => {
    baseline = null;
    settle();
  };

  settle();
  const vv = window.visualViewport;
  window.addEventListener('focusout', settle);
  window.addEventListener('orientationchange', onOrientationChange);
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
    window.removeEventListener('orientationchange', onOrientationChange);
    window.removeEventListener('resize', settle);
    window.removeEventListener('pageshow', settle);
    document.removeEventListener('visibilitychange', onVisibilityChange);
    vv?.removeEventListener('resize', settle);
    vv?.removeEventListener('scroll', settle);
    probe.remove();
    debugPanel?.remove();
    root.style.removeProperty('--app-height');
    for (const edge of EDGES) root.style.removeProperty(`--safe-${edge}`);
  };
}
