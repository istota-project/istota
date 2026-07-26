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
 * stylesheet). They are re-measured only when no text-entry element holds focus
 * — i.e. never mid-keyboard. Rotation is handled, since focus is elsewhere by
 * the time it settles.
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
function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
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
  let timer: ReturnType<typeof setTimeout> | undefined;

  const standalone = isStandalone();

  function measure(): void {
    if (isTextEntryFocused()) return;
    if (standalone) root.style.setProperty('--app-height', `${window.innerHeight}px`);
    const cs = getComputedStyle(probe);
    for (const edge of EDGES) {
      const value = cs.getPropertyValue(`padding-${edge}`);
      // An all-zero reading while the keyboard is animating out would latch the
      // wrong thing; those arrive with focus still set, so the guard above has
      // already returned. A genuine 0 (desktop, notchless device) is written and
      // is simply what env() would have produced anyway.
      if (value) root.style.setProperty(`--safe-${edge}`, value);
    }
  }

  // iOS settles the viewport after the events stop, so re-measure on a short
  // tail rather than only on the event itself.
  function schedule(): void {
    measure();
    clearTimeout(timer);
    timer = setTimeout(measure, 400);
  }

  schedule();
  const vv = window.visualViewport;
  window.addEventListener('focusout', schedule);
  window.addEventListener('orientationchange', schedule);
  window.addEventListener('resize', schedule);
  vv?.addEventListener('resize', schedule);

  return () => {
    clearTimeout(timer);
    window.removeEventListener('focusout', schedule);
    window.removeEventListener('orientationchange', schedule);
    window.removeEventListener('resize', schedule);
    vv?.removeEventListener('resize', schedule);
    probe.remove();
    root.style.removeProperty('--app-height');
    for (const edge of EDGES) root.style.removeProperty(`--safe-${edge}`);
  };
}
