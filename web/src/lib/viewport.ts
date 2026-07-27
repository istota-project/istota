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
import { onKeyboardGeometry, shellAtLeast, shellVersion } from './platform/native';

const EDGES = ['top', 'bottom', 'left', 'right'] as const;

/**
 * Third job, and the only one that is not a workaround: take the soft
 * keyboard's height from the native shell.
 *
 * Everything above infers the keyboard from viewports that move underneath it.
 * The shell does not have to infer anything — iOS tells it how tall the
 * keyboard will be and when, and Capacitor forwards that as a `keyboardWillShow`
 * event at the *start* of the animation. Published as `--kb-height`, which
 * `app.css` subtracts from the app's height, so the composer is above the
 * keyboard before the keys have finished sliding up.
 *
 * Gated on the shell version, because until 0.2.0 the shell resized the WebView
 * itself (`resize: native`) and an offset applied here as well would count the
 * keyboard twice. That native resize is also why this is worth doing: Capacitor
 * defers it until the keyboard's animation has finished *plus 200ms*
 * (`Keyboard.m:256`), then applies it in one jump, which is the visible lurch
 * this replaces. Shell 0.2.0 sets `resize: none` and hands the job here.
 *
 * A browser never sees these events, so nothing about it changes.
 */
const SHELL_WEB_MANAGED_KEYBOARD = '0.2.0';

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

function nextBaseline(prev: Baseline | null, now: Baseline, replace: boolean): Baseline {
  if (!prev || replace) return now;
  return { inner: Math.max(prev.inner, now.inner), visual: Math.max(prev.visual, now.visual) };
}

/**
 * The baseline, kept across a reload.
 *
 * The baseline is the whole defence against a viewport iOS has left short, and
 * it is built purely by observation — so it is only as good as this page's
 * memory of a taller reading. A reload wipes that memory while the platform
 * state survives it: the new document is handed the same slightly-short layout
 * viewport the old one was left with, nothing afterwards restores those pixels,
 * and the first reading becomes the tallest ever seen. The app is then pinned
 * short for the life of the page — the band, back for good, and reachable
 * without touching the keyboard again: type once, then tap the update prompt.
 *
 * So the baseline is written to sessionStorage and read back at startup.
 * Session scope is the point. It covers a reload, which is where the stale
 * platform reading lives, and it is gone by the next cold launch, which starts
 * from a fresh WebView with nothing to carry forward.
 *
 * Keyed on the layout width and the display mode, since those are what change
 * when the geometry legitimately changes — rotation, split view, a tab versus
 * the installed app. A record that does not match is ignored rather than
 * trusted. A stored baseline that is somehow still too tall costs nothing
 * either: it reads as short, so nothing is published at all and the app falls
 * back to the `dvh` default until the adopt path replaces it.
 */
const BASELINE_KEY = 'istota_viewport_baseline';

type StoredBaseline = Baseline & { width: number; pwa: boolean };

function readStoredBaseline(pwa: boolean): Baseline | null {
  try {
    const raw = sessionStorage.getItem(BASELINE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Partial<StoredBaseline> | null;
    if (!stored || stored.width !== window.innerWidth || stored.pwa !== pwa) return null;
    if (typeof stored.inner !== 'number' || typeof stored.visual !== 'number') return null;
    if (!(stored.inner > 0)) return null;
    return { inner: stored.inner, visual: stored.visual };
  } catch {
    return null;
  }
}

function writeStoredBaseline(baseline: Baseline, pwa: boolean): void {
  try {
    const record: StoredBaseline = { ...baseline, width: window.innerWidth, pwa };
    sessionStorage.setItem(BASELINE_KEY, JSON.stringify(record));
  } catch {
    // Private mode, quota, a disabled store — the in-memory baseline still
    // does its job for this page, it just will not outlive it.
  }
}

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
    // design-lint-allow: dev-only debug overlay, deliberately not themed.
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
  let baseline: Baseline | null = readStoredBaseline(standalone);
  let sessionKind: 'blur' | 'other' = 'other';
  const written = new Map<string, string>();
  const debugPanel = debugEnabled() ? makeDebugPanel() : null;

  function paintDebug(state: string): void {
    if (!debugPanel) return;
    const vv = window.visualViewport;
    const el = document.scrollingElement ?? document.documentElement;
    debugPanel.textContent = [
      `${state}/${sessionKind}${standalone ? ' pwa' : ''}${isTextEntryFocused() ? ' focus' : ''}`,
      `inner ${window.innerHeight}  vv ${vv ? Math.round(vv.height * vv.scale) : '-'}`,
      `base  ${baseline ? `${baseline.inner}/${Math.round(baseline.visual)}` : '-'}`,
      `off   ${vv ? Math.round(vv.offsetTop) : '-'}  scroll ${Math.round(el?.scrollTop ?? 0)}`,
      `app   ${written.get('--app-height') ?? '-'}  sb ${written.get('--safe-bottom') ?? '-'}`,
      // The shell's own line: whether it was recognised at all, and whether its
      // keyboard reports are landing. A version with no height behind it after
      // the keyboard is up means the event arrived in a shape this did not
      // read — which is exactly how the first version of this failed.
      `shell ${shellVersion() ?? '-'}  kb ${written.get('--kb-height') ?? '-'}`,
    ].join('\n');
  }

  function write(prop: string, value: string): void {
    if (written.get(prop) === value) return;
    written.set(prop, value);
    root.style.setProperty(prop, value);
  }

  /**
   * One measurement pass. Returns a fingerprint of everything published (null if
   * nothing was) and whether the layout viewport is below its baseline.
   */
  function measure(adoptShort: boolean): { reading: string | null; short: boolean } {
    const geo = currentGeometry();
    // Below the last keyboard-free layout viewport: either a keyboard still on
    // its way out, or a genuinely smaller window (rotation, iPad split view).
    // Which one it is only becomes clear from whether it comes back.
    const short = baseline !== null && geo.inner < baseline.inner - RESTORED_TOLERANCE_PX;
    const parts: string[] = [];

    // The app height and the residual scroll are both properties of the *layout*
    // viewport, so they are gated on that alone rather than on the keyboard
    // verdict: a whole layout viewport is safe to publish even while the visual
    // one is still short, which is the state iOS 26 can leave behind for good.
    // A short one is never published — holding the last whole height leaves the
    // app slightly too tall for a moment, where publishing a keyboard-shaped one
    // leaves a band at the bottom that nothing later corrects.
    if (!short || adoptShort) {
      releaseResidualScroll();
      if (standalone) {
        // Pinned to the tallest the layout viewport has been, not to this
        // reading. A reading that has come back to within the tolerance but not
        // all the way is still a band at the bottom, just a smaller one — and
        // the height the app *should* be is a thing we already know. Only the
        // adopt path (a shrink that outlasted the whole window with no keyboard
        // behind it) is allowed to publish something shorter.
        const height = `${adoptShort ? geo.inner : Math.max(geo.inner, baseline?.inner ?? geo.inner)}px`;
        write('--app-height', height);
        parts.push(height);
      }
    }

    // The insets keep the conservative gate. They read 0 while the keyboard is
    // up, and latching that is the failure this module exists to prevent.
    if (!keyboardIsUp(baseline)) {
      const cs = getComputedStyle(probe);
      for (const edge of EDGES) {
        const value = cs.getPropertyValue(`padding-${edge}`);
        if (!value) continue;
        write(`--safe-${edge}`, value);
        parts.push(value);
      }
    }
    return { reading: parts.length ? parts.join('|') : null, short };
  }

  function tick(): void {
    timer = undefined;
    const elapsed = Date.now() - startedAt;
    const expired = elapsed >= SETTLE_MAX_MS;
    // A viewport that has stayed short for the whole window, in a session no
    // keyboard was involved in, is the real geometry — adopt it. A session that
    // began with a blur is a keyboard dismissal by definition, so a short
    // reading there is never adopted however long it persists.
    const adoptShort = expired && sessionKind !== 'blur' && !isTextEntryFocused();
    const { reading, short } = measure(adoptShort);

    if (reading === null) {
      // Nothing publishable — the keyboard is up and the layout viewport with
      // it. Keep sampling rather than waiting for an event, since the event
      // that would restart us is exactly what a stuck transition fails to fire.
      paintDebug('held');
      if (!expired) timer = setTimeout(tick, SETTLE_INTERVAL_MS);
      return;
    }

    if (reading === lastReading) stableTicks += 1;
    else {
      lastReading = reading;
      stableTicks = 1;
    }

    const settled = !short && stableTicks >= SETTLE_STABLE_TICKS && elapsed >= SETTLE_MIN_MS;
    if (settled || adoptShort) {
      // The reference the rest of this file measures against, taken only from a
      // settled reading with nothing focused — so a value still moving through a
      // keyboard transition can never become it. It only ever grows, except on
      // rotation (cleared) and on the adopt path (replaced outright): a keyboard
      // can only ever make a viewport smaller, so a smaller reading is never
      // evidence about how tall the app should be.
      if (!isTextEntryFocused()) {
        baseline = nextBaseline(baseline, currentGeometry(), adoptShort);
        writeStoredBaseline(baseline, standalone);
      }
      paintDebug(adoptShort ? 'adopted' : 'settled');
      return;
    }
    paintDebug(short ? 'short' : 'sampling');
    if (expired) return;
    timer = setTimeout(tick, SETTLE_INTERVAL_MS);
  }

  /**
   * (Re)start a settle session. Cheap to call from every event that could mean
   * the viewport moved — a session in progress is simply extended.
   *
   * A session that begins with a blur is a keyboard dismissal, and stays marked
   * as one until it ends: everything that follows it (the resizes, the visual
   * viewport pans) belongs to that dismissal, so a short reading during it is
   * never mistaken for a smaller window. Which is the case the two ways of
   * dismissing a keyboard separated — the accessory bar's Done restores the
   * viewport fast enough to look settled, tapping outside does not.
   */
  function settle(kind: 'blur' | 'other' = 'other'): void {
    clearTimeout(timer);
    // Don't let a follow-on event downgrade an in-flight dismissal session.
    if (kind === 'blur' || Date.now() - startedAt >= SETTLE_MAX_MS) sessionKind = kind;
    startedAt = Date.now();
    stableTicks = 0;
    lastReading = '';
    tick();
  }

  /**
   * The shell's keyboard, applied as it happens.
   *
   * Deliberately not routed through the settle machinery: that exists to find
   * the truth in readings that lie, and this number does not lie. It is written
   * straight through, in one pass with the inset, so the layout moves once.
   *
   * `--safe-bottom` goes to 0 for the duration. The inset is held at its
   * home-indicator value through a keyboard transition on purpose (see the top
   * of this file), but a keyboard is physically over the home indicator, so
   * holding it there is a dead strip between the composer and the keys. The
   * latched value is put back on the way out — the measurement it came from
   * cannot be retaken while the keyboard is up.
   */
  let latchedSafeBottom: string | null = null;

  function applyKeyboardHeight(height: number): void {
    if (height > 0) {
      if (latchedSafeBottom === null) latchedSafeBottom = written.get('--safe-bottom') ?? '';
      write('--kb-height', `${height}px`);
      write('--safe-bottom', '0px');
      paintDebug('keyboard');
      return;
    }
    write('--kb-height', '0px');
    if (latchedSafeBottom) write('--safe-bottom', latchedSafeBottom);
    latchedSafeBottom = null;
    // The dismissal still needs the settle pass: it is what re-measures the
    // insets and unwinds any scroll iOS applied to track the caret.
    settle('blur');
  }

  const stopKeyboard = shellAtLeast(SHELL_WEB_MANAGED_KEYBOARD)
    ? onKeyboardGeometry(applyKeyboardHeight)
    : () => {};

  const onFocusOut = () => settle('blur');
  const onSettle = () => settle();

  const onVisibilityChange = () => {
    if (document.visibilityState === 'visible') settle();
  };

  // Rotation changes both viewports legitimately, so the old keyboard-free
  // reference is meaningless; drop it and let the session re-establish one.
  const onOrientationChange = () => {
    baseline = null;
    sessionKind = 'other';
    settle();
  };

  settle();
  const vv = window.visualViewport;
  window.addEventListener('focusout', onFocusOut);
  window.addEventListener('orientationchange', onOrientationChange);
  window.addEventListener('resize', onSettle);
  window.addEventListener('pageshow', onSettle);
  document.addEventListener('visibilitychange', onVisibilityChange);
  vv?.addEventListener('resize', onSettle);
  // The keyboard pans the visual viewport as well as resizing it, and on the way
  // out the pan is sometimes the only event that fires.
  vv?.addEventListener('scroll', onSettle);

  return () => {
    clearTimeout(timer);
    stopKeyboard();
    window.removeEventListener('focusout', onFocusOut);
    window.removeEventListener('orientationchange', onOrientationChange);
    window.removeEventListener('resize', onSettle);
    window.removeEventListener('pageshow', onSettle);
    document.removeEventListener('visibilitychange', onVisibilityChange);
    vv?.removeEventListener('resize', onSettle);
    vv?.removeEventListener('scroll', onSettle);
    probe.remove();
    debugPanel?.remove();
    root.style.removeProperty('--app-height');
    root.style.removeProperty('--kb-height');
    for (const edge of EDGES) root.style.removeProperty(`--safe-${edge}`);
  };
}
