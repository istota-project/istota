/**
 * The native iOS shell, as the web app sees it.
 *
 * The shell (the `istota-mobile` repo) is a Capacitor WebView pointed at this
 * deployment, so it serves these exact bytes — there is no mobile build of this
 * app and there is not going to be one. What the shell adds is capabilities a
 * webview does not have, and this module is the only place that knows about
 * them. Components ask for a capability; they never mention Capacitor.
 *
 * Two rules keep that honest, and both exist because the two sides ship on
 * different clocks — this app deploys in minutes, the shell binary lags a
 * TestFlight cycle behind it:
 *
 * 1. Everything here degrades to a no-op or the plain web behaviour. A browser
 *    running this code must be indistinguishable from one running the version
 *    before it.
 * 2. Anything that depends on shell behaviour is gated on the shell version
 *    that introduced it, so a deploy that runs ahead of the app is inert rather
 *    than broken.
 *
 * The shell announces itself by appending `IstotaApp/<version>` to the User
 * Agent (`capacitor.config.ts`, `ios.appendUserAgent`). That is deliberately
 * the signal rather than `window.Capacitor`: it is present on the very first
 * request, it survives SSR, and it carries the version, which the injected
 * bridge does not.
 */

const UA_TOKEN = /(?:^|\s)IstotaApp\/(\S+)/;

/** The shell's version, or null in an ordinary browser. */
export function shellVersion(): string | null {
  if (typeof navigator === 'undefined') return null;
  return UA_TOKEN.exec(navigator.userAgent)?.[1] ?? null;
}

export function isNativeShell(): boolean {
  return shellVersion() !== null;
}

/**
 * Is this the given shell version or newer? False in a browser.
 *
 * The gate for every capability whose *shell* half has to be present for the
 * web half to be correct. Compared component-wise: `0.10.0` is newer than
 * `0.9.0`, which a string comparison gets backwards. A non-numeric tail on a
 * component (`0.2.0-beta.1`) is ignored rather than treated as a parse failure,
 * since it orders after the release by every convention we would use.
 */
export function shellAtLeast(version: string): boolean {
  const have = shellVersion();
  if (have === null) return false;

  const mine = have.split('.');
  const want = version.split('.');
  for (let i = 0; i < Math.max(mine.length, want.length); i++) {
    const a = parseInt(mine[i] ?? '0', 10) || 0;
    const b = parseInt(want[i] ?? '0', 10) || 0;
    if (a !== b) return a > b;
  }
  return true;
}

/**
 * The soft keyboard's height, reported as it starts to animate.
 *
 * Capacitor's Keyboard plugin mirrors its `keyboardWillShow`/`keyboardWillHide`
 * events onto `window` for Cordova compatibility, which is why this needs no
 * Capacitor dependency at all — a plain listener and a documented event name.
 * `will`, not `did`: these fire at the *start* of the keyboard's animation, so
 * a layout driven from them moves with the keyboard instead of after it.
 *
 * The handler is called with 0 when the keyboard goes away, so a caller has one
 * code path for both directions. Off-shell nothing ever dispatches these, so
 * subscribing is harmless.
 */
type NativeEvent = Event & {
  keyboardHeight?: number;
  detail?: { keyboardHeight?: number } | null;
};

export function onKeyboardGeometry(handler: (height: number) => void): () => void {
  if (typeof window === 'undefined') return () => {};

  const onShow = (e: Event) => {
    const ev = e as NativeEvent;
    // Capacitor does not use CustomEvent: it builds a plain Event and copies
    // the payload's properties straight onto it (`native-bridge.js`,
    // `createEvent`), so the height is on the event, not under `detail`. The
    // `detail` read stays as a fallback — it is the shape the plugin's own
    // typings describe, and the one a future bridge would most likely move to.
    const height = ev.keyboardHeight ?? ev.detail?.keyboardHeight;
    handler(typeof height === 'number' && height > 0 ? height : 0);
  };
  const onHide = () => handler(0);

  window.addEventListener('keyboardWillShow', onShow);
  window.addEventListener('keyboardWillHide', onHide);

  return () => {
    window.removeEventListener('keyboardWillShow', onShow);
    window.removeEventListener('keyboardWillHide', onHide);
  };
}
