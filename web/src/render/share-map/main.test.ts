/**
 * The share-map renderer as the browser service drives it: import the entry,
 * then call `window.istotaRender(payload)` once.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { waitFor } from '@testing-library/svelte';
import type { ApiDouble } from '$lib/test/apiDouble';
import type { RenderResult } from '../host';

// MapLibre needs WebGL, which jsdom lacks; replaced at the module boundary.
const ml = vi.hoisted(() => {
  type Handler = (ev?: unknown) => void;
  const maps: FakeMap[] = [];
  class FakeMap {
    options: Record<string, unknown>;
    handlers = new Map<string, Handler[]>();
    constructor(options: Record<string, unknown>) {
      this.options = options;
      maps.push(this);
    }
    on(name: string, fn: Handler) {
      this.handlers.set(name, [...(this.handlers.get(name) ?? []), fn]);
      return this;
    }
    once(name: string, fn: Handler) {
      return this.on(name, fn);
    }
    fire(name: string, ev?: unknown) {
      for (const fn of this.handlers.get(name) ?? []) fn(ev);
    }
    addSource() {}
    addLayer() {}
    addControl() {}
    areTilesLoaded() {
      return true;
    }
    resize() {}
    remove() {}
  }
  class AttributionControl {
    onAdd() {
      return document.createElement('div');
    }
    onRemove() {}
  }
  class NavigationControl {}
  return { maps, module: { default: { Map: FakeMap, NavigationControl, AttributionControl } } };
});
vi.mock('maplibre-gl', () => ml.module);

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const { fillApiDouble } = await import('$lib/test/apiDouble');
await fillApiDouble(api);

type RenderWindow = Window & { istotaRender?: (payload: unknown) => Promise<RenderResult> };

function observation(overrides: Record<string, unknown> = {}) {
  return {
    precision: 'exact',
    lat: 52.52,
    lon: 13.405,
    accuracy_m: 40,
    observed_at: '2026-10-16T14:02:31+00:00',
    freshness: 'fresh',
    owner_user_id: 'bob',
    ...overrides,
  };
}

function payload(overrides: Record<string, unknown> = {}) {
  return {
    observation: observation(),
    basemap: {
      provider: 'osm',
      kind: 'raster',
      dark: 'https://tiles.example.test/dark/{z}/{x}/{y}.png',
      light: 'https://tiles.example.test/light/{z}/{x}/{y}.png',
      attribution: 'OSM contributors',
    },
    owner: 'Bob',
    viewport: { width: 640, height: 360 },
    ...overrides,
  };
}

async function loadEntry(): Promise<(raw: unknown) => Promise<RenderResult>> {
  await import('./main');
  const fn = (window as RenderWindow).istotaRender;
  if (!fn) throw new Error('the entry registered no renderer');
  return fn;
}

const fetchSpy = vi.fn();

beforeEach(() => {
  vi.resetModules();
  ml.maps.length = 0;
  document.body.innerHTML = '<div data-render-frame></div>';
  document.documentElement.setAttribute('data-theme', 'dark');
  vi.stubGlobal('fetch', fetchSpy);
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  delete (window as RenderWindow).istotaRender;
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
  fetchSpy.mockReset();
});

describe('the share-map renderer', () => {
  it('draws the payload as a static light map and resolves ok once it is idle', async () => {
    const render = await loadEntry();
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');

    const result = render(payload());
    await waitFor(() => expect(ml.maps).toHaveLength(1));
    const map = ml.maps[0];
    expect(map.options.interactive).toBe(false);
    const style = map.options.style as {
      layers: { id: string; source?: string; layout?: { visibility: string } }[];
      sources: Record<string, { tiles: string[] }>;
    };
    const light = style.layers.find((l) => l.id === 'basemap-light');
    expect(light?.layout?.visibility).toBe('visible');
    expect(style.sources[light?.source ?? ''].tiles[0]).toContain('/light/');

    const frame = document.querySelector<HTMLElement>('[data-render-frame]');
    expect(frame?.style.width).toBe('640px');
    expect(frame?.style.height).toBe('360px');
    expect(frame?.querySelector('figure')).not.toBeNull();
    expect(frame?.textContent).toContain('Observed 2026-10-16 14:02 UTC');

    map.fire('load');
    map.fire('idle');
    await expect(result).resolves.toEqual({ ok: true });
    // The page asks istota nothing: the basemap was the payload's.
    expect(api.basemapOnce).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("passes the map's own failure back as the result", async () => {
    const render = await loadEntry();
    const result = render(payload());
    await waitFor(() => expect(ml.maps).toHaveLength(1));
    ml.maps[0].fire('error', {});
    await expect(result).resolves.toEqual({ ok: false, reason: 'style_failed' });
  });

  it('refuses a latitude Web Mercator cannot draw, without a map', async () => {
    const render = await loadEntry();
    await expect(render(payload({ observation: observation({ lat: 86 }) }))).resolves.toEqual({
      ok: false,
      reason: 'not_mercator_safe',
    });
    expect(ml.maps).toHaveLength(0);
  });

  it('refuses a payload with no basemap rather than fetching one', async () => {
    const render = await loadEntry();
    await expect(render(payload({ basemap: undefined }))).resolves.toEqual({
      ok: false,
      reason: 'bad_payload',
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(ml.maps).toHaveLength(0);
    expect(api.basemapOnce).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
