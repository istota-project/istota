/**
 * Stub for SvelteKit's `$service-worker` under vitest.
 *
 * The real module is generated per build and only resolvable inside the worker
 * bundle, so the routing tests get a fixed manifest instead: one hashed bundle
 * entry, one prerendered document per shape the router branches on, and the
 * static directory as it actually stands — including `robots.txt`, which is
 * what makes the precache allowlist testable.
 */
export const base = '/istota';

export const build = [
  '/istota/_app/immutable/entry/app.abc123.js',
  '/istota/_app/immutable/chunks/index.def456.js',
];

export const files = [
  '/istota/manifest.webmanifest',
  '/istota/favicon.png',
  '/istota/logo-192.png',
  '/istota/logo-512.png',
  '/istota/apple-touch-icon.png',
  '/istota/octopus-sigil.webp',
  '/istota/robots.txt',
];

// With the trailing slash Kit actually emits, which is the whole reason the
// navigation fallback tries more than one spelling of a path.
export const prerendered = ['/istota/', '/istota/chat/', '/istota/settings/'];

export const version = 'test-version';
