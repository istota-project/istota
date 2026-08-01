import '@testing-library/jest-dom/vitest';
import { afterAll } from 'vitest';

// bits-ui's body scroll lock resets the body style on a *deferred* timer, not
// on unmount: releasing the last lock schedules `resetBodyStyle` for
// `restoreScrollDelay` ms (24 when the caller passes none) so a menu closing
// and another opening in the same tick don't fight over the style. Unmounting
// the last overlay in a test file therefore leaves a timer in flight, and if
// the file ends first, it fires into a torn-down jsdom and throws
// `ReferenceError: document is not defined` as an *unhandled* error — which
// vitest reports against whichever file was running, without failing anything.
// That is the whole flake: intermittent, attributed to an innocent file, and
// invisible in the pass count.
//
// So drain it before teardown. Polling rather than sleeping a fixed 24ms+1,
// because the delay is a caller-supplied value rather than a constant we can
// pin — this waits exactly as long as the pending cleanup actually takes and
// costs nothing in the vast majority of files, which never open an overlay.
//
// The signal is the body's inline style. A lock always writes one
// (`overflow: hidden`, plus `pointer-events: none` a tick later) and the reset
// restores whatever was there before — nothing, in a fresh jsdom. Deliberately
// NOT `--scrollbar-width`, the more specific-looking marker: bits-ui only sets
// that when the viewport actually has a scrollbar, which under jsdom it never
// does, so gating on it would skip every time and fix nothing.
//
// A component still mounted at this point holds its lock and has scheduled
// nothing, so there is no timer to lose — the ceiling is what stops those
// files spinning here for the full budget.
const LOCK_DRAIN_TIMEOUT_MS = 250;
const LOCK_DRAIN_POLL_MS = 5;

afterAll(async () => {
  if (typeof document === 'undefined') return;
  const deadline = Date.now() + LOCK_DRAIN_TIMEOUT_MS;
  while (document.body.getAttribute('style') && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, LOCK_DRAIN_POLL_MS));
  }
});

// jsdom implements no part of the Pointer Capture API, and bits-ui's overlay
// primitives call it unconditionally while opening — so a test that so much as
// clicks a `Select` trigger dies inside the library rather than failing on its
// own assertion. Stubbed as no-ops because capture only decides which element
// keeps receiving a pointer stream jsdom is not simulating anyway.
for (const name of ['setPointerCapture', 'releasePointerCapture'] as const) {
  if (!(name in Element.prototype)) {
    // `writable` defaults to false under defineProperty, which would make a
    // test's own plain reassignment throw in strict mode.
    Object.defineProperty(Element.prototype, name, {
      value: () => {},
      configurable: true,
      writable: true,
    });
  }
}
if (!('hasPointerCapture' in Element.prototype)) {
  Object.defineProperty(Element.prototype, 'hasPointerCapture', {
    value: () => false,
    configurable: true,
    writable: true,
  });
}

// jsdom runs on an opaque origin, where `window.localStorage` is absent — so
// anything reached through `stores/persisted.ts` silently no-ops unless a test
// stands one up. `theme.test.ts` and `fontSize.test.ts` each carried their own
// copy of this; it lives here once instead. (`chat.view.test.ts` is a
// different approach, not a third copy — it mocks the `persisted` module
// itself and never touches storage, so it is unaffected either way.)
if (typeof globalThis.localStorage === 'undefined') {
  const data = new Map<string, string>();
  const stub: Storage = {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => void data.delete(k),
    setItem: (k, v) => void data.set(k, String(v)),
  };
  Object.defineProperty(globalThis, 'localStorage', { value: stub, configurable: true });
}
