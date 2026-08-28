import { beforeEach, describe, expect, it, vi } from 'vitest';

// The basemap spec is per-user — it embeds that user's stored CARTO key in the
// tile URL — and it is cached module-wide for the life of the page. Saving a
// key on /location/settings and going to /location is a client-side
// navigation, so without invalidation the map keeps serving the stale spec and
// the key appears to do nothing until a full reload.

const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const SPEC_A = { provider: 'openfreemap', kind: 'style', dark: 'a', light: 'a' };
const SPEC_B = { provider: 'carto', kind: 'raster', dark: 'b?api_key=k', light: 'b' };

describe('basemapOnce', () => {
  beforeEach(async () => {
    vi.resetModules();
    fetchMock.mockReset();
  });

  it('fetches once and shares the answer', async () => {
    const api = await import('./api');
    fetchMock.mockResolvedValue(jsonResponse(SPEC_A));

    const [a, b] = await Promise.all([api.basemapOnce(), api.basemapOnce()]);

    expect(a).toEqual(SPEC_A);
    expect(b).toEqual(SPEC_A);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not cache a failure', async () => {
    const api = await import('./api');
    fetchMock.mockRejectedValueOnce(new Error('offline'));
    await expect(api.basemapOnce()).rejects.toThrow();

    fetchMock.mockResolvedValue(jsonResponse(SPEC_A));
    await expect(api.basemapOnce()).resolves.toEqual(SPEC_A);
  });

  it('re-fetches after the cache is reset', async () => {
    const api = await import('./api');
    fetchMock.mockResolvedValue(jsonResponse(SPEC_A));
    await api.basemapOnce();

    api.resetBasemapCache();
    fetchMock.mockResolvedValue(jsonResponse(SPEC_B));

    await expect(api.basemapOnce()).resolves.toEqual(SPEC_B);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('saving a carto key invalidates the cached spec', async () => {
    const api = await import('./api');
    fetchMock.mockResolvedValue(jsonResponse(SPEC_A));
    await api.basemapOnce();

    fetchMock.mockResolvedValue(jsonResponse({ ok: true, configured: true }));
    await api.setSecret('carto', 'api_key', 'mykey');

    fetchMock.mockResolvedValue(jsonResponse(SPEC_B));
    await expect(api.basemapOnce()).resolves.toEqual(SPEC_B);
  });

  it('clearing a carto key invalidates the cached spec', async () => {
    const api = await import('./api');
    fetchMock.mockResolvedValue(jsonResponse(SPEC_B));
    await api.basemapOnce();

    fetchMock.mockResolvedValue(jsonResponse({ ok: true, deleted: true }));
    await api.deleteSecret('carto', 'api_key');

    fetchMock.mockResolvedValue(jsonResponse(SPEC_A));
    await expect(api.basemapOnce()).resolves.toEqual(SPEC_A);
  });

  it('an unrelated secret does not invalidate it', async () => {
    const api = await import('./api');
    fetchMock.mockResolvedValue(jsonResponse(SPEC_A));
    await api.basemapOnce();

    fetchMock.mockResolvedValue(jsonResponse({ ok: true, configured: true }));
    await api.setSecret('overland', 'ingest_token', 'tok');

    fetchMock.mockResolvedValue(jsonResponse(SPEC_B));
    // Still the cached spec: nothing about the basemap changed.
    await expect(api.basemapOnce()).resolves.toEqual(SPEC_A);
  });
});
