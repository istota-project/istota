import '@testing-library/jest-dom/vitest';

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
