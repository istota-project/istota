/**
 * The client half of the notification URL allowlist.
 *
 * The server validates every URL it emits, at runtime, on every view
 * (`notification_sources.invalid_paths`, covered by `test_notification_urls.py`).
 * This is the second copy and not a substitute for it: the **browser** is the
 * side that performs the fetch with the session cookie attached, and the side
 * that follows an anchor — off a path the *server* chose by interpolating an
 * `object_id` that is opaque `TEXT` on the row.
 *
 * It had no test at all, which the spec's own reasoning rules out: it rejects a
 * guard whose test "passes trivially with a benign `object_id` and cannot
 * falsify the property it claims to protect". So the hostile set below is the
 * one the spec names, plus the two the anchor fields add.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { isSafeActionPath, runNotificationAction } from '$lib/api';

const HOSTILE = [
  // Traversal — the shape an opaque `object_id` makes reachable.
  '/chat/tasks/1/../../admin/x',
  '/chat/tasks/1/../..',
  // A query or fragment: this is a path allowlist, not a URL one.
  '/chat/tasks/1/confirm?x=1',
  '/chat/tasks/1/confirm#x',
  // Percent-encoding, which would otherwise smuggle a separator past a naive
  // check and be decoded on the far side.
  '/chat/tasks/1%2F..%2Fadmin/confirm',
  // Off-origin and scheme-bearing. The last two matter most for `href` and
  // `link`, which land in an anchor where a text-node rule buys nothing.
  'https://evil.example/steal',
  '//evil.example/steal',
  'javascript:alert(1)',
  'data:text/html,x',
  // Control characters. The pattern is anchored \A/\Z rather than ^/$ precisely
  // so a trailing newline cannot ride along.
  '/chat/tasks/1/confirm\n',
  '/chat/tasks/1/confirm\r\nX-Injected: 1',
  '/chat/tasks/1/confirm\t',
  // Not a same-origin absolute path at all.
  'chat/tasks/1/confirm',
  '',
  '/',
  // A leading non-alphanumeric after the slash.
  '/./admin',
  '/-x',
  '/_x',
];

const BENIGN = [
  '/notifications/count',
  '/chat/tasks/12/confirm',
  '/chat/tasks/12/cancel',
  '/chat/drafts/7/approve',
  '/chat/drafts/7/discard',
  '/health',
  '/a',
  '/a_b-c/d',
];

describe('isSafeActionPath', () => {
  it.each(BENIGN)('accepts %j', (path) => {
    expect(isSafeActionPath(path)).toBe(true);
  });

  it.each(HOSTILE)('refuses %j', (path) => {
    expect(isSafeActionPath(path)).toBe(false);
  });

  it('refuses null and undefined rather than treating absence as valid', () => {
    // Absence is the caller's business to distinguish. Conflating "no link"
    // with "a valid link" here is how the check ends up skipped on the field
    // that has one.
    expect(isSafeActionPath(null)).toBe(false);
    expect(isSafeActionPath(undefined)).toBe(false);
  });

  it('matches the shape of the server pattern it mirrors', () => {
    // `notification_sources.SAFE_PATH_RE` is \A/[A-Za-z0-9][A-Za-z0-9/_-]*\Z.
    // Restated rather than imported across the language boundary, so this
    // asserts the shape rather than the spelling.
    expect(isSafeActionPath('/Aa0/_-')).toBe(true);
    expect(isSafeActionPath('/0')).toBe(true);
    expect(isSafeActionPath('/a b')).toBe(false);
    expect(isSafeActionPath('/a.b')).toBe(false);
  });
});

describe('runNotificationAction', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('refuses a hostile path without issuing a request', async () => {
    // Refusing *before* the fetch is the whole point: a rejection after the
    // request has left has already sent the session cookie somewhere.
    for (const path of HOSTILE) {
      await expect(runNotificationAction(path)).rejects.toThrow(/unsafe/i);
    }
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('posts a benign path', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }),
    );
    await runNotificationAction('/chat/tasks/12/confirm');
    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const [url, init] = vi.mocked(globalThis.fetch).mock.calls[0];
    expect(String(url)).toContain('/chat/tasks/12/confirm');
    expect(init?.method).toBe('POST');
    // Same-origin only; the session cookie must not ride to a third party.
    expect(init?.credentials).toBe('same-origin');
  });
});
