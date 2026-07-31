import '@testing-library/jest-dom/vitest';

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
