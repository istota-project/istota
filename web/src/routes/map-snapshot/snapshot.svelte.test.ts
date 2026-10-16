/**
 * The page the browser service captures for a location share's PNG.
 *
 * Named without the `+`, like the other route tests beside their pages:
 * SvelteKit reserves that prefix.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';
import type { ApiDouble } from '$lib/test/apiDouble';

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

import Page from './+page.svelte';

type SnapshotResult = { ok: boolean; reason?: string };
type SnapshotWindow = Window & { renderSnapshot?: (payload: unknown) => Promise<SnapshotResult> };

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
      needs_key: false,
      fell_back: false,
      warning: '',
    },
    owner: 'Bob',
    ...overrides,
  };
}

function renderSnapshot(raw: unknown): Promise<SnapshotResult> {
  const fn = (window as SnapshotWindow).renderSnapshot;
  if (!fn) throw new Error('renderSnapshot is not installed');
  return fn(raw);
}

const fetchSpy = vi.fn();

beforeEach(() => {
  ml.maps.length = 0;
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
  cleanup();
  vi.unstubAllGlobals();
  fetchSpy.mockReset();
});

describe('the map snapshot page', () => {
  it('shows an empty 640 x 360 frame when loaded alone, and asks the server nothing', async () => {
    const { container } = render(Page);
    const frame = container.querySelector<HTMLElement>('[data-snapshot-frame]');
    expect(frame).not.toBeNull();
    expect(frame?.style.width).toBe('640px');
    expect(frame?.style.height).toBe('360px');
    expect(frame?.querySelector('figure')).toBeNull();
    await new Promise((r) => setTimeout(r, 20));
    expect(ml.maps).toHaveLength(0);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(api.basemapOnce).not.toHaveBeenCalled();
  });

  it('forces the light theme, whatever the stored preference', () => {
    render(Page);
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('draws the payload as a static light map and resolves ok once it is idle', async () => {
    const { container } = render(Page);
    const result = renderSnapshot(payload());

    await waitFor(() => expect(ml.maps).toHaveLength(1));
    const map = ml.maps[0];
    expect(map.options.interactive).toBe(false);
    // The basemap is the payload's, light flavour, with no API call.
    const style = map.options.style as {
      layers: { id: string; source?: string; layout?: { visibility: string } }[];
      sources: Record<string, { tiles: string[] }>;
    };
    const light = style.layers.find((l) => l.id === 'basemap-light');
    expect(light?.layout?.visibility).toBe('visible');
    expect(style.sources[light?.source ?? ''].tiles[0]).toContain('/light/');
    expect(api.basemapOnce).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();

    const frame = container.querySelector<HTMLElement>('[data-snapshot-frame]');
    expect(frame?.querySelector('figure')).not.toBeNull();
    expect(frame?.textContent).toContain('Observed 2026-10-16 14:02 UTC');

    map.fire('load');
    map.fire('idle');
    await expect(result).resolves.toEqual({ ok: true });
  });

  it('passes a map failure back as the result', async () => {
    render(Page);
    const result = renderSnapshot(payload());
    await waitFor(() => expect(ml.maps).toHaveLength(1));
    ml.maps[0].fire('error', {});
    await expect(result).resolves.toEqual({ ok: false, reason: 'style_failed' });
  });

  it('refuses a latitude Web Mercator cannot draw, without a map', async () => {
    render(Page);
    await expect(
      renderSnapshot(payload({ observation: observation({ lat: 86 }) })),
    ).resolves.toEqual({ ok: false, reason: 'not_mercator_safe' });
    expect(ml.maps).toHaveLength(0);
  });

  it('refuses a malformed payload, and one with no basemap, rather than fetching', async () => {
    render(Page);
    await expect(renderSnapshot({ observation: {} })).resolves.toEqual({
      ok: false,
      reason: 'bad_payload',
    });
    await expect(renderSnapshot(payload({ basemap: undefined }))).resolves.toEqual({
      ok: false,
      reason: 'bad_payload',
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(ml.maps).toHaveLength(0);
    expect(api.basemapOnce).not.toHaveBeenCalled();
  });

  it('renders once per page load', async () => {
    render(Page);
    void renderSnapshot(payload());
    await expect(renderSnapshot(payload())).resolves.toEqual({
      ok: false,
      reason: 'already_rendered',
    });
    await waitFor(() => expect(ml.maps).toHaveLength(1));
  });

  it('removes renderSnapshot when the page goes away', () => {
    const { unmount } = render(Page);
    expect(typeof (window as SnapshotWindow).renderSnapshot).toBe('function');
    unmount();
    expect((window as SnapshotWindow).renderSnapshot).toBeUndefined();
  });
});
