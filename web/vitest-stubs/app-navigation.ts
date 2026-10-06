/** SvelteKit navigation double. Shallow writes change state, never page.url. */
import { page } from './app-state.svelte';

type Entry = { url: string; state: Record<string, unknown> };

// History entries are serialized by the browser, not live references to page.state.
function copyState(state: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(state));
}

function show(entry: Entry): void {
  window.history.replaceState(null, '', entry.url);
  page.state = copyState(entry.state);
}

export const __history = {
  entries: [] as Entry[],
  index: -1,
  reset(url = '/istota/', state: Record<string, unknown> = {}) {
    this.entries = [{ url, state: copyState(state) }];
    this.index = 0;
    show(this.entries[0]);
    page.url = new URL(window.location.href);
  },
  back() {
    if (this.index > 0) show(this.entries[--this.index]);
  },
  forward() {
    if (this.index + 1 < this.entries.length) show(this.entries[++this.index]);
  },
};

function write(url: string | URL, state: Record<string, unknown>, replace: boolean): void {
  if (__history.index < 0) __history.reset(window.location.href, page.state);
  const entry = { url: String(url), state: copyState(state) };
  if (replace) {
    __history.entries[__history.index] = entry;
  } else {
    __history.entries.splice(__history.index + 1);
    __history.entries.push(entry);
    __history.index++;
  }
  show(entry);
}

export function pushState(url: string | URL, state: Record<string, unknown>): void {
  write(url, state, false);
}

export function replaceState(url: string | URL, state: Record<string, unknown>): void {
  write(url, state, true);
}

export function goto(_url: string): Promise<void> {
  return Promise.resolve();
}

export function invalidateAll(): Promise<void> {
  return Promise.resolve();
}

export function afterNavigate(_callback: () => void): void {
  // Shallow routing never invokes afterNavigate.
}
