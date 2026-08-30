/**
 * The connectivity store: what it believes, and who is allowed to tell it.
 *
 * Every case here is about *authority* rather than about a happy path. The
 * store's whole reason to exist is that the three available signals disagree —
 * an interface can be up with no server behind it (a captive portal), a request
 * can fail for reasons that prove the server is there (a 400, a 401), and the
 * one signal that is a fact about the server is the one nothing else reports.
 * So the tests are mostly "this input must NOT move the store".
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({ getChatConfig: vi.fn() }));
vi.mock('$lib/api', () => api);

type Store = typeof import('./connectivity');

let store: Store;
let stop: (() => void) | null = null;

/** jsdom's `navigator.onLine` is a prototype getter; shadow it per test. */
function setNavigatorOnLine(value: boolean): void {
  Object.defineProperty(window.navigator, 'onLine', { value, configurable: true });
}

function setVisibility(value: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { value, configurable: true });
}

beforeEach(async () => {
  api.getChatConfig.mockReset();
  api.getChatConfig.mockResolvedValue({});
  setNavigatorOnLine(true);
  setVisibility('visible');
  vi.useFakeTimers();
  vi.resetModules();
  store = await import('./connectivity');
});

afterEach(() => {
  stop?.();
  stop = null;
  vi.useRealTimers();
});

describe('noteTransport', () => {
  it('starts online, so nothing reports a gap before a request has seen one', () => {
    expect(get(store.online)).toBe(true);
  });

  it('goes offline on an unreachable send', () => {
    store.noteTransport(false, 'unreachable');
    expect(get(store.online)).toBe(false);
  });

  it('goes offline on a timeout', () => {
    // Ambiguous for the *message* — the task may exist — but not for the
    // connection: nothing answered inside the bound.
    store.noteTransport(false, 'timeout');
    expect(get(store.online)).toBe(false);
  });

  it('stays online through a rejection, because the server answered', () => {
    store.noteTransport(false, 'rejected');
    expect(get(store.online)).toBe(true);
  });

  it('comes back online on a rejection, because the server answered', () => {
    store.noteTransport(false, 'unreachable');
    store.noteTransport(false, 'rejected');
    expect(get(store.online)).toBe(true);
  });

  it('treats auth and rate_limit as evidence of a server', () => {
    store.noteTransport(false, 'unreachable');
    store.noteTransport(false, 'auth');
    expect(get(store.online)).toBe(true);

    store.noteTransport(false, 'unreachable');
    store.noteTransport(false, 'rate_limit');
    expect(get(store.online)).toBe(true);
  });

  it('says nothing on an unclassified failure', () => {
    // A caller that could not tell what it saw must not be able to raise the
    // banner on a guess.
    store.noteTransport(false, 'unreachable');
    store.noteTransport(false);
    expect(get(store.online)).toBe(false);

    store.noteTransport(true);
    store.noteTransport(false);
    expect(get(store.online)).toBe(true);
  });

  it('comes back online on a successful request and stops probing', async () => {
    stop = store.startConnectivity();
    store.noteTransport(false, 'unreachable');
    store.noteTransport(true);
    expect(get(store.online)).toBe(true);

    await vi.advanceTimersByTimeAsync(store.PROBE_BACKOFF_MS[0] * 20);
    expect(api.getChatConfig).not.toHaveBeenCalled();
  });
});

describe('navigator.onLine', () => {
  it('forces offline immediately when the interface is already down at start', () => {
    setNavigatorOnLine(false);
    stop = store.startConnectivity();
    expect(get(store.online)).toBe(false);
  });

  it('forces offline on the offline event, with no probe to wait for', () => {
    stop = store.startConnectivity();
    window.dispatchEvent(new Event('offline'));
    expect(get(store.online)).toBe(false);
    expect(api.getChatConfig).not.toHaveBeenCalled();
  });

  it('does not flip online on the online event until a probe succeeds', async () => {
    stop = store.startConnectivity();
    store.noteTransport(false, 'unreachable');

    // The interface is back. On iOS that regularly means a captive portal, so
    // it buys a probe and nothing else.
    let release: (v: unknown) => void = () => {};
    api.getChatConfig.mockReturnValue(new Promise((resolve) => (release = resolve)));
    window.dispatchEvent(new Event('online'));
    expect(api.getChatConfig).toHaveBeenCalledTimes(1);
    expect(get(store.online)).toBe(false);

    release({});
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.online)).toBe(true);
  });

  it('stays offline when the probe fired by the online event fails', async () => {
    stop = store.startConnectivity();
    store.noteTransport(false, 'unreachable');
    api.getChatConfig.mockRejectedValue(new Error('unreachable'));

    window.dispatchEvent(new Event('online'));
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.online)).toBe(false);
  });

  it('probes when the app comes back to the foreground', async () => {
    stop = store.startConnectivity();
    store.noteTransport(false, 'unreachable');

    setVisibility('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(api.getChatConfig).not.toHaveBeenCalled();

    setVisibility('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getChatConfig).toHaveBeenCalledTimes(1);
    expect(get(store.online)).toBe(true);
  });

  it('does not probe on a foreground while it believes it is online', () => {
    stop = store.startConnectivity();
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('online'));
    expect(api.getChatConfig).not.toHaveBeenCalled();
  });
});

describe('the probe backoff', () => {
  it('runs 5/10/20/40/60 and then holds at a minute', async () => {
    api.getChatConfig.mockRejectedValue(new Error('unreachable'));
    stop = store.startConnectivity();
    store.noteTransport(false, 'unreachable');

    const expected = [5_000, 10_000, 20_000, 40_000, 60_000, 60_000];
    for (const [i, delay] of expected.entries()) {
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(api.getChatConfig).toHaveBeenCalledTimes(i);
      await vi.advanceTimersByTimeAsync(1);
      expect(api.getChatConfig).toHaveBeenCalledTimes(i + 1);
    }
  });

  it('bounds the probe, so a stalled connection cannot park it forever', async () => {
    api.getChatConfig.mockRejectedValue(new Error('unreachable'));
    stop = store.startConnectivity();
    store.noteTransport(false, 'unreachable');
    await vi.advanceTimersByTimeAsync(store.PROBE_BACKOFF_MS[0]);
    expect(api.getChatConfig).toHaveBeenCalledWith(store.PROBE_TIMEOUT_MS);
  });

  it('resets on a transition, so a flap does not inherit the last schedule', async () => {
    api.getChatConfig.mockRejectedValue(new Error('unreachable'));
    stop = store.startConnectivity();
    store.noteTransport(false, 'unreachable');

    // Four failures in, the schedule is out at 60s.
    await vi.advanceTimersByTimeAsync(5_000 + 10_000 + 20_000 + 40_000);
    expect(api.getChatConfig).toHaveBeenCalledTimes(4);

    api.getChatConfig.mockResolvedValue({});
    await vi.advanceTimersByTimeAsync(60_000);
    expect(get(store.online)).toBe(true);

    // Offline again: the next probe is 5s away, not 60.
    api.getChatConfig.mockRejectedValue(new Error('unreachable'));
    api.getChatConfig.mockClear();
    store.noteTransport(false, 'unreachable');
    await vi.advanceTimersByTimeAsync(5_000);
    expect(api.getChatConfig).toHaveBeenCalledTimes(1);
  });

  it('runs one probe at a time, however many triggers arrive', async () => {
    let release: (v: unknown) => void = () => {};
    api.getChatConfig.mockReturnValue(new Promise((resolve) => (release = resolve)));
    stop = store.startConnectivity();
    store.noteTransport(false, 'unreachable');

    window.dispatchEvent(new Event('online'));
    window.dispatchEvent(new Event('online'));
    document.dispatchEvent(new Event('visibilitychange'));
    expect(api.getChatConfig).toHaveBeenCalledTimes(1);

    release({});
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.online)).toBe(true);
  });

  it('does not probe before it is started', async () => {
    store.noteTransport(false, 'unreachable');
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(api.getChatConfig).not.toHaveBeenCalled();
  });
});

describe('teardown', () => {
  it('cancels the schedule and drops every listener', async () => {
    api.getChatConfig.mockRejectedValue(new Error('unreachable'));
    const teardown = store.startConnectivity();
    store.noteTransport(false, 'unreachable');
    await vi.advanceTimersByTimeAsync(store.PROBE_BACKOFF_MS[0]);
    expect(api.getChatConfig).toHaveBeenCalledTimes(1);

    teardown();
    api.getChatConfig.mockClear();

    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(api.getChatConfig).not.toHaveBeenCalled();

    window.dispatchEvent(new Event('online'));
    document.dispatchEvent(new Event('visibilitychange'));
    expect(api.getChatConfig).not.toHaveBeenCalled();

    store.noteTransport(true);
    window.dispatchEvent(new Event('offline'));
    expect(get(store.online)).toBe(true);
  });
});
