/** Stub for SvelteKit's `$app/state` under vitest. */
export const page = $state({
  url: new URL('http://localhost/'),
  params: {} as Record<string, string>,
  route: { id: null as string | null },
  state: {} as Record<string, unknown>,
});

/**
 * Kit's stale-build signal, which the root layout reads to show its reload
 * prompt. Always false here: a test mounting the layout is testing something
 * else, and a `true` would paint a toast over everything it asserts on.
 */
export const updated = {
  current: false,
  check: async () => false,
};
