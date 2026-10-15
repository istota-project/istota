import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, { getNavUnreadCounts: vi.fn() });
let store: typeof import('./navUnread');
let connectivity: typeof import('./connectivity');
const empty = { chat: null, briefings: null, feeds: null };
const summary = { enabled: true, chat: 4, briefings: 2, feeds: 108 };
function deferred() {
  let resolve!: (value: typeof summary) => void;
  const promise = new Promise<typeof summary>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function visibility(value: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value });
  document.dispatchEvent(new Event('visibilitychange'));
}
beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  visibility('visible');
  api.getNavUnreadCounts.mockReset().mockResolvedValue(summary);
  connectivity = await import('./connectivity');
  store = await import('./navUnread');
});
afterEach(() => {
  store?.stopNavUnreadPoll();
  vi.useRealTimers();
});

describe('navigation unread polling', () => {
  it('starts unknown, runs once per interval, and has one owner', async () => {
    expect(get(store.navUnreadCounts)).toEqual(empty);
    store.invalidateNavUnreadCounts();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.getNavUnreadCounts).not.toHaveBeenCalled();
    store.startNavUnreadPoll('alice');
    store.startNavUnreadPoll('alice');
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.navUnreadCounts)).toEqual({ chat: 4, briefings: 2, feeds: 108 });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(2);
    store.stopNavUnreadPoll();
    expect(get(store.navUnreadCounts)).toEqual(empty);
    await vi.advanceTimersByTimeAsync(300_000);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(2);
  });

  it('coalesces mutations, discards a pre-read response and makes one trailing request', async () => {
    const pending = deferred();
    api.getNavUnreadCounts.mockReturnValueOnce(pending.promise);
    const seen: (number | null)[] = [];
    const unsubscribe = store.navUnreadCounts.subscribe((value) => seen.push(value.chat));
    store.startNavUnreadPoll('alice');
    store.invalidateNavUnreadCounts();
    store.invalidateNavUnreadCounts();
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(1);
    api.getNavUnreadCounts.mockResolvedValue({ ...summary, chat: 0 });
    pending.resolve(summary);
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.navUnreadCounts).chat).toBe(0);
    expect(seen).not.toContain(4);
    unsubscribe();
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(2);
    store.invalidateNavUnreadCounts();
    store.invalidateNavUnreadCounts();
    await vi.advanceTimersByTimeAsync(999);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(3);
  });

  it.each(['hidden', 'offline'])('clears on %s and refreshes once on return', async (pause) => {
    store.startNavUnreadPoll('alice');
    await vi.advanceTimersByTimeAsync(0);
    if (pause === 'hidden') visibility('hidden');
    else connectivity.noteTransport(false, 'unreachable');
    expect(get(store.navUnreadCounts)).toEqual(empty);
    store.invalidateNavUnreadCounts();
    await vi.advanceTimersByTimeAsync(90_000);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(1);
    if (pause === 'hidden') visibility('visible');
    else connectivity.noteTransport(true);
    window.dispatchEvent(new Event('focus'));
    visibility('visible');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(2);
  });

  it.each(['hidden', 'offline'])('waits to fetch when started %s', async (pause) => {
    if (pause === 'hidden') visibility('hidden');
    else connectivity.noteTransport(false, 'unreachable');
    store.startNavUnreadPoll('alice');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(api.getNavUnreadCounts).not.toHaveBeenCalled();
    if (pause === 'hidden') visibility('visible');
    else connectivity.noteTransport(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getNavUnreadCounts).toHaveBeenCalledOnce();
  });

  it('does not publish a response from before hiding, even after returning', async () => {
    const pending = deferred();
    api.getNavUnreadCounts.mockReturnValueOnce(pending.promise);
    store.startNavUnreadPoll('alice');
    visibility('hidden');
    visibility('visible');
    api.getNavUnreadCounts.mockResolvedValue({ ...summary, feeds: 1 });
    pending.resolve(summary);
    await vi.advanceTimersByTimeAsync(1000);
    expect(get(store.navUnreadCounts).feeds).toBe(1);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(2);
  });

  it('rejects the previous account response and never overlaps requests', async () => {
    const pending = deferred();
    api.getNavUnreadCounts.mockReturnValueOnce(pending.promise);
    store.startNavUnreadPoll('alice');
    store.startNavUnreadPoll('bob');
    expect(get(store.navUnreadCounts)).toEqual(empty);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(1);
    api.getNavUnreadCounts.mockResolvedValue({ ...summary, chat: 1 });
    pending.resolve(summary);
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.navUnreadCounts).chat).toBe(1);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(2);
  });

  it('ignores late results after disable and starts fresh when enabled again', async () => {
    const pending = deferred();
    api.getNavUnreadCounts.mockReturnValueOnce(pending.promise);
    store.startNavUnreadPoll('alice');
    store.stopNavUnreadPoll();
    pending.resolve(summary);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(get(store.navUnreadCounts)).toEqual(empty);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(1);
    store.startNavUnreadPoll('alice');
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.navUnreadCounts).feeds).toBe(108);
  });

  it.each(['disabled', 'auth'])(
    'stops on %s until a new live identity confirms the preference',
    async (kind) => {
      if (kind === 'auth') api.getNavUnreadCounts.mockRejectedValue(new api.AuthError());
      else api.getNavUnreadCounts.mockResolvedValue({ enabled: false, ...empty });
      store.startNavUnreadPoll('alice');
      await vi.advanceTimersByTimeAsync(0);
      expect(get(store.navUnreadCounts)).toEqual(empty);
      store.invalidateNavUnreadCounts();
      visibility('hidden');
      visibility('visible');
      await vi.advanceTimersByTimeAsync(300_000);
      expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(1);
      api.getNavUnreadCounts.mockResolvedValue(summary);
      store.startNavUnreadPoll('alice');
      await vi.advanceTimersByTimeAsync(0);
      expect(get(store.navUnreadCounts).chat).toBe(4);
    },
  );

  it('keeps healthy sections when another is unavailable', async () => {
    api.getNavUnreadCounts.mockResolvedValue({ ...summary, feeds: null });
    store.startNavUnreadPoll('alice');
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.navUnreadCounts)).toEqual({ chat: 4, briefings: 2, feeds: null });
  });

  it.each([-1, 0.5, '3', undefined, NaN])('rejects malformed counts: %s', async (chat) => {
    api.getNavUnreadCounts.mockResolvedValue({ ...summary, chat } as never);
    store.startNavUnreadPoll('alice');
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.navUnreadCounts)).toEqual(empty);
  });

  it('hides failed results, backs off after five failures, and resumes immediately', async () => {
    store.startNavUnreadPoll('alice');
    await vi.advanceTimersByTimeAsync(0);
    api.getNavUnreadCounts.mockRejectedValue(new Error('unavailable'));
    await vi.advanceTimersByTimeAsync(30_000 * 5);
    expect(get(store.navUnreadCounts)).toEqual(empty);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(6);
    await vi.advanceTimersByTimeAsync(299_999);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(6);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(7);
    api.getNavUnreadCounts.mockResolvedValue(summary);
    visibility('hidden');
    visibility('visible');
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.navUnreadCounts).chat).toBe(4);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.getNavUnreadCounts).toHaveBeenCalledTimes(9);
  });
});
