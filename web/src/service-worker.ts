/// <reference types="@sveltejs/kit" />
/**
 * The offline app shell (ISSUE-202).
 *
 * The iOS app does not bundle this build: its WebView is pointed at the
 * deployment, so the WebView origin *is* the site origin — which is what keeps
 * the session cookie, the OAuth redirect chain and both SSE streams working
 * unmodified. The cost is that a cold launch with no connection never gets a
 * document at all: WebKit paints its own "cannot connect to the server" page
 * and no web-side code runs, so the IndexedDB cache and the outbox that the
 * rest of this feature is built on are unreachable. A service worker is the
 * only thing that can answer that navigation, which is the whole reason this
 * file exists.
 *
 * Three properties are load-bearing, and each is a decision rather than an
 * implementation detail:
 *
 * 1. **Nothing under `${base}/api/` is ever cached.** Data caching is the
 *    app's job, in IndexedDB (`lib/offline/db.ts`), where it is keyed by user,
 *    bounded per room, and read by code that knows what a stale message means.
 *    A `Cache` full of authenticated JSON would be a second, dumber cache that
 *    fights the first one, survives a logout, and answers a 401-worthy request
 *    with the previous session's data.
 * 2. **An `Accept: text/event-stream` request is passed through untouched**,
 *    whatever its path. An SSE response never ends, so there is nothing to put
 *    in a cache and reading the body to clone it would stall the stream.
 * 3. **`version.json` stays on the network**, or the stale-build prompt in
 *    `routes/+layout.svelte` stops firing and this worker pins the app to the
 *    build it installed with.
 *
 * Registration is deliberately *not* automatic — `kit.serviceWorker.register`
 * is false in `svelte.config.js` and `+layout.svelte` registers this only
 * inside the native shell. A service worker is the one client artifact that can
 * pin a continuously deployed app to a stale build, so it is confined to the
 * surface that needs it and that has a native escape hatch.
 *
 * Typed by hand rather than through `lib.webworker`: the worker's own module is
 * pulled into the app's TypeScript program by its test, and the two lib sets
 * collide when they meet. What is declared below is only what this file uses.
 */
import { base, build, files, prerendered, version } from '$service-worker';

interface ExtendableEventLike {
  waitUntil(promise: Promise<unknown>): void;
}

interface FetchEventLike extends ExtendableEventLike {
  request: Request;
  respondWith(response: Response | Promise<Response>): void;
}

interface WorkerGlobal {
  location: { origin: string };
  clients: { claim(): Promise<void> };
  addEventListener(
    type: 'install' | 'activate',
    handler: (event: ExtendableEventLike) => void,
  ): void;
  addEventListener(type: 'fetch', handler: (event: FetchEventLike) => void): void;
}

const sw = self as unknown as WorkerGlobal;

/**
 * One cache per build, so activating a new worker retires the old one whole.
 *
 * `version` is Kit's build version — the same value `version.json` publishes,
 * which is what the update prompt compares against.
 */
const CACHE = `istota-${version}`;

/**
 * Everything from `static/` that the shell cannot render without.
 *
 * An allowlist rather than all of `files`: `cache.addAll` is all-or-nothing, so
 * a precache that grows silently with the static directory is one that
 * eventually fails to install and takes offline boot with it. The sigil is
 * here because it is in the nav on every route, and a broken image in the
 * header is how an offline launch would otherwise announce itself.
 */
const STATIC_ALLOWLIST = [
  'manifest.webmanifest',
  'favicon.png',
  'logo-192.png',
  'logo-512.png',
  'apple-touch-icon.png',
  'octopus-sigil.webp',
];

/**
 * What an installed generation cannot run without: the hashed bundle and the
 * allowlisted static files.
 */
const ESSENTIAL_URLS: string[] = [
  ...new Set([
    ...build,
    ...STATIC_ALLOWLIST.map((name) => `${base}/${name}`).filter((url) => files.includes(url)),
  ]),
];

/**
 * The precache manifest: the essentials above plus every prerendered document.
 *
 * The documents are the half that makes an offline *navigation* resolve —
 * without them the bundle is cached and unreachable, because the request that
 * fails is the one for the page.
 */
export const PRECACHE_URLS: string[] = [...new Set([...ESSENTIAL_URLS, ...prerendered])];

const PRECACHED = new Set(PRECACHE_URLS);

/** How the router says a request should be answered. */
export type Strategy = 'passthrough' | 'cache-first' | 'network-first' | 'network-only';

/** Only what the router reads, so a test can hand it a navigation. */
interface RoutableRequest {
  method: string;
  url: string;
  mode: string;
  headers: { get(name: string): string | null };
}

/**
 * Which of the four strategies a request gets. Pure, and the whole of the
 * policy — the handler below only carries it out.
 *
 * Order matters: the passthrough tests come first, so nothing later can claim
 * a request that must not be touched.
 */
export function routeFor(request: RoutableRequest, origin: string): Strategy {
  if (request.method !== 'GET') return 'passthrough';

  let url: URL;
  try {
    url = new URL(request.url);
  } catch {
    return 'passthrough';
  }
  if (url.origin !== origin) return 'passthrough';

  if (request.headers.get('accept')?.includes('text/event-stream')) return 'passthrough';
  if (url.pathname.startsWith(`${base}/api/`)) return 'passthrough';

  // Content-hashed, so a cache hit is by construction the right bytes.
  if (url.pathname.startsWith(`${base}/_app/immutable/`)) return 'cache-first';

  // The document. Network first so a deploy is picked up on the next launch,
  // with a short timeout because a stalled connection is the case this is for.
  if (request.mode === 'navigate') return 'network-first';

  if (PRECACHED.has(url.pathname)) return 'cache-first';

  // Everything else, `version.json` included, is asked of the network and
  // fails as it would with no worker at all.
  return 'network-only';
}

/**
 * How long a navigation waits for the network before the cache answers.
 *
 * A cold launch in airplane mode fails fast, but the case this bounds is the
 * other one — a connection that is up and going nowhere, where `fetch` can
 * hang for far longer than a person will wait at a blank screen.
 */
export const NAVIGATION_TIMEOUT_MS = 3000;

function withTimeout(promise: Promise<Response>, ms: number): Promise<Response> {
  return new Promise<Response>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

async function cacheFirst(request: Request): Promise<Response> {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request, { ignoreSearch: true });
  if (hit) return hit;
  const response = await fetch(request);
  // A miss on a hashed asset means a build newer than this worker, so it is
  // worth keeping for the launch after this one. Only a plain 200: a partial
  // or opaque response cached here would be served as if it were whole.
  if (response.status === 200 && response.type !== 'opaque') {
    await cache.put(request, response.clone()).catch(() => {});
  }
  return response;
}

/**
 * Which cached entries could answer a navigation to `pathname`, best first.
 *
 * **The trailing slash is the whole reason this is a list.** Kit's
 * `prerendered` names each document with one (`/istota/chat/`), which is what
 * the install caches it under, while every link in the app points at the same
 * route without one (`/istota/chat`) — so matching the request's path alone
 * misses every document in the cache and a cold launch offline falls all the
 * way through to the root. Both spellings are tried, in that order.
 *
 * `${base}/index.html` is the spec's named fallback and is kept for a
 * deployment that serves it; the entry that actually exists here is the
 * prerendered root, so that is last.
 */
export function documentCacheKeys(pathname: string): string[] {
  const withSlash = pathname.endsWith('/') ? pathname : `${pathname}/`;
  const withoutSlash = pathname.endsWith('/') ? pathname.slice(0, -1) : pathname;
  const keys = [pathname, withSlash, withoutSlash, `${base}/index.html`, `${base}/`];
  return [...new Set(keys.filter(Boolean))];
}

/**
 * The document: the network, then this build's cached copy of that route, then
 * the app's root document.
 *
 * The cached document is copied into a fresh `Response` before it is handed
 * back. A response the install fetched through a redirect is marked as such,
 * and answering a *navigation* with one throws in the page ("a redirected
 * response was used for a request whose redirect mode is not follow") — which
 * would turn the one case this exists for into a blank screen. Copying costs a
 * few kilobytes and removes the mark.
 */
async function navigationFirst(request: Request): Promise<Response> {
  const cache = await caches.open(CACHE);
  try {
    return await withTimeout(fetch(request), NAVIGATION_TIMEOUT_MS);
  } catch {
    const url = new URL(request.url);
    for (const key of documentCacheKeys(url.pathname)) {
      const hit = await cache.match(key);
      if (hit) return new Response(hit.body, { status: 200, headers: hit.headers });
    }
    return new Response('Offline, and this page is not saved on this device.', {
      status: 503,
      headers: { 'content-type': 'text/plain; charset=utf-8' },
    });
  }
}

async function networkOnly(request: Request): Promise<Response> {
  try {
    return await fetch(request);
  } catch {
    return new Response('Offline', {
      status: 503,
      headers: { 'content-type': 'text/plain; charset=utf-8' },
    });
  }
}

sw.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      // The bundle and the icons are all-or-nothing: `addAll` rejects the
      // whole install if one entry fails, which is right for the files an
      // installed worker cannot run without — a half-precached generation
      // would boot to a broken page rather than to WebKit's error page.
      await cache.addAll(ESSENTIAL_URLS);
      // The documents are added one at a time and tolerantly. There are forty
      // of them, one per route, and a deployment that answers one of them
      // differently — a redirect rule, a route added since the last deploy —
      // must cost that route's offline boot rather than the feature.
      await Promise.allSettled(prerendered.map((url) => cache.add(url)));
    })(),
  );
});

sw.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // This app's own generations only. Nothing else on the origin keeps a
      // cache today, and a worker that deletes storage it did not write is one
      // more thing to remember when something later does.
      for (const key of await caches.keys()) {
        if (key.startsWith('istota-') && key !== CACHE) await caches.delete(key);
      }
      await sw.clients.claim();
    })(),
  );
});

sw.addEventListener('fetch', (event) => {
  const strategy = routeFor(event.request, sw.location.origin);
  if (strategy === 'passthrough') return;
  if (strategy === 'cache-first') {
    event.respondWith(cacheFirst(event.request));
    return;
  }
  if (strategy === 'network-first') {
    event.respondWith(navigationFirst(event.request));
    return;
  }
  event.respondWith(networkOnly(event.request));
});
