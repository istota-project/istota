import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { SearchResponse } from '$lib/api';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import { createSearch } from './search';
beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
const response = (query: string): SearchResponse => ({ query, groups: [], on_demand: [] });
it('debounces and never searches a single character', async () => {
  api.search.mockResolvedValue(response('hello'));
  const store = createSearch();
  store.setQuery('h');
  await vi.advanceTimersByTimeAsync(200);
  expect(api.search).not.toHaveBeenCalled();
  store.setQuery('he');
  await vi.advanceTimersByTimeAsync(100);
  store.setQuery('hello');
  await vi.advanceTimersByTimeAsync(199);
  expect(api.search).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  expect(api.search).toHaveBeenCalledExactlyOnceWith(
    'hello',
    expect.objectContaining({ signal: expect.any(AbortSignal) }),
  );
  store.close();
});
it('aborts and drops late old responses even while the next query debounces', async () => {
  let finish!: (r: SearchResponse) => void;
  api.search.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const store = createSearch();
  store.setQuery('old');
  await vi.advanceTimersByTimeAsync(200);
  const signal = api.search.mock.calls[0][1].signal;
  store.setQuery('new');
  finish({
    ...response('old'),
    groups: [
      {
        source: 'chats',
        label: 'Old',
        results: [],
        has_more: false,
        relaxed: false,
        error: null,
        elapsed_ms: 0,
      },
    ],
  });
  await Promise.resolve();
  expect(signal.aborted).toBe(true);
  expect(get(store).results).toEqual([]);
  store.close();
});
it('records recents only on result opening and tolerates unavailable storage', async () => {
  api.search.mockResolvedValue(response('hello'));
  const store = createSearch();
  store.setQuery('hello');
  await vi.advanceTimersByTimeAsync(200);
  expect(localStorage.getItem('istota.search.recent')).toBeNull();
  store.rememberQuery();
  expect(JSON.parse(localStorage.getItem('istota.search.recent')!)).toEqual(['hello']);
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new Error('disabled');
  });
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new Error('disabled');
  });
  expect(() => createSearch().rememberQuery()).not.toThrow();
  expect(() => store.rememberQuery()).not.toThrow();
  store.close();
});
