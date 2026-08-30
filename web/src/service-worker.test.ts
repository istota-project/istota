/**
 * What the service worker does and does not intercept (ISSUE-202).
 *
 * The worker exists for one case — a cold launch of the iOS app with no
 * connection, where nothing web-side runs because WebKit never got a document
 * — and the part of it worth a test is the routing decision, because that is
 * the part that would do damage if it were wrong. A worker that answered an
 * API call from a cache would serve one user's authenticated JSON to whoever
 * asked next, and one that touched a `text/event-stream` response would park
 * a stream that never ends in storage.
 *
 * So: the table in the spec, asserted as a table. Precaching, install and
 * activate are exercised on device (the spec's Stage 6 matrix), not here —
 * mocking `caches` well enough to prove them would be a test of the mock.
 */
import { describe, it, expect } from 'vitest';
import { base, build, files, prerendered } from '$service-worker';
import { routeFor, documentCacheKeys, PRECACHE_URLS, type Strategy } from './service-worker';

const ORIGIN = 'https://example.test';

/**
 * A request, as the router reads one.
 *
 * Plain objects rather than `new Request(...)`: `mode: 'navigate'` cannot be
 * constructed by hand at all (the fetch spec reserves it for the browser's own
 * navigations), which is exactly the branch that most needs covering.
 */
function req(
  path: string,
  opts: { method?: string; mode?: string; accept?: string; origin?: string } = {},
) {
  const accept = opts.accept ?? null;
  return {
    method: opts.method ?? 'GET',
    url: `${opts.origin ?? ORIGIN}${path}`,
    mode: opts.mode ?? 'cors',
    headers: { get: (name: string) => (name.toLowerCase() === 'accept' ? accept : null) },
  };
}

const route = (path: string, opts?: Parameters<typeof req>[1]): Strategy =>
  routeFor(req(path, opts), ORIGIN);

describe('the service worker router — what it leaves alone', () => {
  it('passes anything that is not a GET straight through', () => {
    expect(route(`${base}/chat`, { method: 'POST', mode: 'navigate' })).toBe('passthrough');
  });

  it('passes a cross-origin request through', () => {
    // Nextcloud's OAuth host, an avatar, anything at all: not ours to cache,
    // and a redirect chain that a worker joined in on is one more thing that
    // can break sign-in.
    expect(route('/index.php/login', { origin: 'https://cloud.example.test' })).toBe('passthrough');
  });

  it('never touches an event stream', () => {
    // Both room and task streams are SSE. A response that never ends cannot
    // be cached, and a worker that so much as reads the body to clone it
    // stalls the stream it copied.
    expect(route(`${base}/api/chat/stream`, { accept: 'text/event-stream' })).toBe('passthrough');
    // The header decides, not the path — an SSE endpoint added outside /api/
    // must be left alone on the same grounds.
    expect(route(`${base}/events`, { accept: 'text/event-stream' })).toBe('passthrough');
  });

  it('never touches the API', () => {
    // The one rule the whole design rests on: data caching is the app's job,
    // in IndexedDB, keyed by user and bounded per room. A second cache here
    // would answer a logged-out request with the last session's JSON.
    expect(route(`${base}/api/chat/config`)).toBe('passthrough');
    expect(route(`${base}/api/chat/rooms/1/messages?limit=50`)).toBe('passthrough');
    expect(route(`${base}/api/me`)).toBe('passthrough');
  });
});

describe('the service worker router — what it serves', () => {
  it('serves the hashed bundle from the cache first', () => {
    expect(route(`${base}/_app/immutable/entry/app.abc123.js`)).toBe('cache-first');
  });

  it('serves a navigation from the network first, falling back to the cache', () => {
    expect(route(`${base}/chat`, { mode: 'navigate' })).toBe('network-first');
    expect(route(`${base}/chat?room=t1`, { mode: 'navigate' })).toBe('network-first');
  });

  it('serves a precached static file from the cache first', () => {
    expect(route(`${base}/favicon.png`)).toBe('cache-first');
    expect(route(`${base}/manifest.webmanifest`)).toBe('cache-first');
  });

  it('leaves the build-version poll on the network', () => {
    // `version.json` is what tells a long-lived session that a new build
    // shipped. Cached, the update prompt would stop firing and the worker
    // would pin the app to the build it installed with — the one failure mode
    // that made registration native-shell-only in the first place.
    expect(route(`${base}/_app/version.json`)).toBe('network-only');
  });

  it('sends anything else to the network', () => {
    expect(route(`${base}/robots.txt`)).toBe('network-only');
    expect(route(`${base}/some/thing/nobody/precached`)).toBe('network-only');
  });
});

describe('what the worker precaches', () => {
  it('takes the whole bundle and every prerendered document', () => {
    for (const url of build) expect(PRECACHE_URLS).toContain(url);
    for (const url of prerendered) expect(PRECACHE_URLS).toContain(url);
  });

  it('takes an allowlist from the static directory rather than all of it', () => {
    expect(PRECACHE_URLS).toContain(`${base}/manifest.webmanifest`);
    expect(PRECACHE_URLS).toContain(`${base}/favicon.png`);
    expect(PRECACHE_URLS).toContain(`${base}/octopus-sigil.webp`);
    // `static/` grows; a precache that silently grows with it is one that
    // eventually fails to install, and `cache.addAll` is all-or-nothing.
    expect(PRECACHE_URLS).not.toContain(`${base}/robots.txt`);
    expect(PRECACHE_URLS.length).toBeLessThan(build.length + prerendered.length + files.length);
  });

  it('lists each entry once', () => {
    expect(new Set(PRECACHE_URLS).size).toBe(PRECACHE_URLS.length);
  });
});

/**
 * Which cached document answers a navigation the network could not.
 *
 * The trailing slash is the whole of it. Kit names a prerendered document with
 * one and every link in the app points at the route without one, so a lookup
 * on the request's own path misses every document in the cache — a cold launch
 * offline would fall through to the root page whatever route it was on, which
 * is one indistinguishable step from not working at all.
 */
describe('the navigation fallback chain', () => {
  it('tries the path as asked, then the way it is actually cached', () => {
    const keys = documentCacheKeys(`${base}/chat`);
    expect(keys[0]).toBe(`${base}/chat`);
    expect(keys[1]).toBe(`${base}/chat/`);
    expect(prerendered).toContain(keys[1]);
  });

  it('works the other way round too', () => {
    expect(documentCacheKeys(`${base}/chat/`)).toEqual([
      `${base}/chat/`,
      `${base}/chat`,
      `${base}/index.html`,
    ]);
  });

  it('never falls back to the app root for a path the build does not carry', () => {
    // The app navigates out of itself to server-rendered routes — sign-in,
    // sign-out, the OAuth starts — and none of them is prerendered. A chain
    // ending at the root would answer a slow sign-in with a 200 carrying the
    // app shell, painting the app at /login with the session unresolved.
    for (const path of ['/login', '/logout', '/reconnect', '/google/connect']) {
      const keys = documentCacheKeys(`${base}${path}`);
      expect(keys).not.toContain(`${base}/`);
      for (const key of keys) expect(PRECACHE_URLS).not.toContain(key);
    }
  });
});
