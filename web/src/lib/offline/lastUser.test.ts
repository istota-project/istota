/**
 * The `chat.lastUserId` pointer (ISSUE-202).
 *
 * One property carries the whole safety argument and it is the one asserted
 * hardest here: the pointer is **read only inside the native shell**. Off the
 * shell the per-user namespace on every cache key is guarding a real hazard —
 * a browser profile two people take turns using — and a pointer that answered
 * there would hand the last user's cached transcript to whoever opened the tab
 * next. Writing it everywhere is harmless and deliberate: it costs one string
 * and it means the shell finds one already there the first time it looks.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { LAST_USER_KEY, forgetLastUserId, rememberLastUserId, seedUserId } from './lastUser';

const SHELL_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) IstotaApp/0.10.0';
const BROWSER_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15';

function useAgent(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}

beforeEach(() => {
  localStorage.clear();
  useAgent(BROWSER_UA);
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  useAgent(BROWSER_UA);
});

describe('the last-user pointer', () => {
  it('round-trips the id inside the shell', () => {
    rememberLastUserId('alice');
    useAgent(SHELL_UA);
    expect(seedUserId()).toBe('alice');
  });

  it('reads as nothing in a browser, whatever is stored', () => {
    rememberLastUserId('alice');
    expect(localStorage.getItem(LAST_USER_KEY)).toBe('"alice"');
    expect(seedUserId()).toBeNull();
  });

  it('is cleared by forgetting it, which is the 401 path', () => {
    rememberLastUserId('alice');
    forgetLastUserId();
    useAgent(SHELL_UA);
    expect(seedUserId()).toBeNull();
  });

  it('treats a config with no user id as no pointer at all', () => {
    rememberLastUserId('alice');
    rememberLastUserId(null);
    useAgent(SHELL_UA);
    expect(seedUserId()).toBeNull();
  });

  it('refuses anything stored that is not a non-empty string', () => {
    useAgent(SHELL_UA);
    for (const raw of ['""', '17', '{"user":"alice"}', '[]', 'not json at all']) {
      localStorage.setItem(LAST_USER_KEY, raw);
      expect(seedUserId()).toBeNull();
    }
  });

  it('says nothing rather than throwing when storage refuses', () => {
    // Private mode and a full quota both surface as a throwing localStorage,
    // and `persisted.ts` swallows it — the pointer degrades to no cold-launch
    // paint rather than taking the load with it.
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('refused');
      },
      setItem: () => {
        throw new Error('refused');
      },
    });
    useAgent(SHELL_UA);
    expect(() => rememberLastUserId('alice')).not.toThrow();
    expect(seedUserId()).toBeNull();
  });
});
