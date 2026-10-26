import { afterEach, expect, it, vi } from 'vitest';
import { AuthError } from '$lib/api';
import { AppsApiError, createPending, deletePending, getApps } from './api';

function answer(status: number, body?: unknown) {
  const fetch = vi.fn(
    async () =>
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
  );
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

afterEach(() => vi.unstubAllGlobals());

it('asks for the list at the router root, with the trailing slash', async () => {
  const fetch = answer(200, { apps: [], pending: [], drafts: [], starter: [], kit_built: true });
  await getApps();
  expect(fetch).toHaveBeenCalledWith('/api/apps/', expect.objectContaining({}));
});

it('carries a validation failure as its list of errors', async () => {
  answer(422, { errors: ['manifest.toml: name is required.', 'index.html is missing.'] });
  const refusal = await createPending('tides').catch((e) => e);
  expect(refusal).toBeInstanceOf(AppsApiError);
  expect(refusal.status).toBe(422);
  expect(refusal.errors).toEqual(['manifest.toml: name is required.', 'index.html is missing.']);
});

it('carries a refusal with a message and no errors', async () => {
  answer(503, { error: 'the apps kit is not built', reason: 'no manifest.json' });
  const refusal = await createPending('tides').catch((e) => e);
  expect(refusal).toBeInstanceOf(AppsApiError);
  expect(refusal.message).toBe('the apps kit is not built');
  expect(refusal.errors).toEqual([]);
});

it('leaves an expired session to the shared AuthError', async () => {
  answer(401, { detail: 'Not authenticated' });
  await expect(getApps()).rejects.toBeInstanceOf(AuthError);
});

it('takes a 204 as success with nothing to read', async () => {
  answer(204);
  await expect(deletePending('tides', 'p1')).resolves.toBeUndefined();
});
