import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import type { ApiDouble } from '$lib/test/apiDouble';
import type { BasemapSpec } from '$lib/basemap';
import type { ShareObservation } from '$lib/location/shareMap';

// MapLibre needs WebGL, which jsdom lacks, so it is replaced at the module
// boundary. The factory waits on a gate the first test opens, which is how the
// test sees the component render before the dynamic import resolves.
const ml = vi.hoisted(() => {
  let open: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    open = resolve;
  });
  type Handler = (ev?: unknown) => void;
  const maps: FakeMap[] = [];
  class FakeMap {
    options: Record<string, unknown>;
    handlers = new Map<string, Handler[]>();
    sources: string[] = [];
    layers: { id: string; type: string }[] = [];
    controls: unknown[] = [];
    removed = false;
    disableRotation = vi.fn();
    touchZoomRotate = { disableRotation: this.disableRotation };
    constructor(options: Record<string, unknown>) {
      if (ml.throwOnConstruct) throw new Error('Failed to initialize WebGL');
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
    off() {
      return this;
    }
    fire(name: string, ev?: unknown) {
      for (const fn of this.handlers.get(name) ?? []) fn(ev);
    }
    addSource(id: string) {
      this.sources.push(id);
    }
    addLayer(layer: { id: string; type: string }) {
      this.layers.push(layer);
    }
    addControl(control: unknown) {
      this.controls.push(control);
    }
    areTilesLoaded() {
      return true;
    }
    resize() {}
    remove() {
      this.removed = true;
    }
  }
  class NavigationControl {
    constructor(public options: unknown) {}
  }
  class AttributionControl {
    removed = false;
    constructor(public options: unknown) {}
    onAdd() {
      const el = document.createElement('div');
      el.textContent = 'Basemap attribution';
      return el;
    }
    onRemove() {
      this.removed = true;
    }
  }
  return {
    gate,
    open: () => open(),
    maps,
    throwOnConstruct: false,
    module: { default: { Map: FakeMap, NavigationControl, AttributionControl } },
  };
});

vi.mock('maplibre-gl', async () => {
  await ml.gate;
  return ml.module;
});

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const { fillApiDouble } = await import('$lib/test/apiDouble');
await fillApiDouble(api);

import ShareMap from './ShareMap.svelte';

const RASTER: BasemapSpec = {
  provider: 'osm',
  kind: 'raster',
  dark: 'https://tiles.example.test/{z}/{x}/{y}.png',
  light: 'https://tiles.example.test/{z}/{x}/{y}.png',
  attribution: 'OSM contributors',
  needs_key: false,
  fell_back: false,
  warning: '',
};

function exact(overrides: Partial<ShareObservation> = {}): ShareObservation {
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

function area(overrides: Partial<ShareObservation> = {}): ShareObservation {
  return exact({ precision: 'area', lat: 52.52, lon: 13.41, accuracy_m: null, ...overrides });
}

async function mounted() {
  await waitFor(() => expect(ml.maps.length).toBe(1));
  return ml.maps[0];
}

beforeEach(() => {
  ml.maps.length = 0;
  ml.throwOnConstruct = false;
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
  vi.useRealTimers();
});

describe('ShareMap', () => {
  it('renders its frame and footer before MapLibre has loaded', async () => {
    render(ShareMap, { observation: exact(), interactive: true, basemap: RASTER });
    expect(screen.getByText('Observed 2026-10-16 14:02 UTC')).toBeTruthy();
    await new Promise((r) => setTimeout(r, 20));
    expect(ml.maps).toHaveLength(0);
    ml.open();
    await mounted();
  });

  it('builds an interactive map: cooperative gestures, no rotate, a compact nav control', async () => {
    render(ShareMap, { observation: exact(), interactive: true, basemap: RASTER });
    const map = await mounted();
    expect(map.options.interactive).toBe(true);
    expect(map.options.cooperativeGestures).toBe(true);
    expect(map.options.dragRotate).toBe(false);
    expect(map.options.attributionControl).toBe(false);
    expect(map.disableRotation).toHaveBeenCalled();
    const nav = map.controls[0] as { options: { showCompass: boolean } };
    expect(nav.options.showCompass).toBe(false);
    expect((map.options.fitBoundsOptions as { maxZoom: number }).maxZoom).toBe(15);
  });

  it('builds a static map: no handlers, a preserved buffer, no fade, no controls', async () => {
    render(ShareMap, { observation: exact(), interactive: false, basemap: RASTER, theme: 'light' });
    const map = await mounted();
    expect(map.options.interactive).toBe(false);
    expect(map.options.fadeDuration).toBe(0);
    expect(map.options.canvasContextAttributes).toMatchObject({ preserveDrawingBuffer: true });
    expect(map.controls).toHaveLength(0);
    // The raster style is built for the theme asked for, not the app's.
    const style = map.options.style as { layers: { id: string; layout: { visibility: string } }[] };
    const light = style.layers.find((l) => l.id === 'basemap-light');
    expect(light?.layout.visibility).toBe('visible');
  });

  it('draws a pin and an accuracy circle for an exact share', async () => {
    const onReady = vi.fn();
    render(ShareMap, { observation: exact(), interactive: false, basemap: RASTER, onReady });
    const map = await mounted();
    map.fire('load');
    const ids = map.layers.map((l) => l.id);
    expect(ids).toContain('share-pin');
    expect(ids).toContain('share-accuracy-fill');
    expect(onReady).not.toHaveBeenCalled();
    map.fire('idle');
    expect(onReady).toHaveBeenCalledExactlyOnceWith({ ok: true });
  });

  it('draws the cell and no pin for an area share, capped at zoom 12', async () => {
    render(ShareMap, { observation: area(), interactive: true, basemap: RASTER });
    const map = await mounted();
    map.fire('load');
    const ids = map.layers.map((l) => l.id);
    expect(ids).toContain('share-area-fill');
    expect(ids).not.toContain('share-pin');
    expect(ids).not.toContain('share-accuracy-fill');
    expect((map.options.fitBoundsOptions as { maxZoom: number }).maxZoom).toBe(12);
    expect(screen.getByText('Approximate area')).toBeTruthy();
  });

  it('puts the attribution, expanded, in the footer', async () => {
    render(ShareMap, { observation: exact(), interactive: true, basemap: RASTER });
    const map = await mounted();
    map.fire('load');
    const footer = document.querySelector('.share-map-footer')!;
    expect(footer.textContent).toContain('Basemap attribution');
    expect(footer.textContent).toContain('Observed 2026-10-16 14:02 UTC');
  });

  it('says last known in the footer and names the map for assistive tech', async () => {
    render(ShareMap, {
      observation: exact({ freshness: 'last_known' }),
      interactive: true,
      basemap: RASTER,
      ownerLabel: 'Bob',
    });
    expect(screen.getByText('Last known, observed 2026-10-16 14:02 UTC')).toBeTruthy();
    expect(
      screen.getByLabelText("Map: Bob's exact location, observed 2026-10-16 14:02 UTC"),
    ).toBeTruthy();
    await mounted();
  });

  it('reports style_failed when the style errors before load', async () => {
    const onReady = vi.fn();
    render(ShareMap, { observation: exact(), interactive: false, basemap: RASTER, onReady });
    const map = await mounted();
    map.fire('error', { error: new Error('style 404') });
    expect(onReady).toHaveBeenCalledExactlyOnceWith({ ok: false, reason: 'style_failed' });
    map.fire('load');
    map.fire('idle');
    expect(onReady).toHaveBeenCalledOnce();
  });

  it('reports tile_error when a tile fails after load', async () => {
    const onReady = vi.fn();
    render(ShareMap, { observation: exact(), interactive: false, basemap: RASTER, onReady });
    const map = await mounted();
    map.fire('load');
    map.fire('error', { error: new Error('tile 500'), sourceId: 'basemap-src-0', tile: {} });
    expect(onReady).toHaveBeenCalledExactlyOnceWith({ ok: false, reason: 'tile_error' });
  });

  it('reports webgl_unavailable when the map cannot be built', async () => {
    ml.throwOnConstruct = true;
    const onReady = vi.fn();
    render(ShareMap, { observation: exact(), interactive: true, basemap: RASTER, onReady });
    await waitFor(() =>
      expect(onReady).toHaveBeenCalledExactlyOnceWith({ ok: false, reason: 'webgl_unavailable' }),
    );
  });

  it('reports timeout when the map is not idle within 8 seconds', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const onReady = vi.fn();
    render(ShareMap, { observation: exact(), interactive: false, basemap: RASTER, onReady });
    const map = await vi.waitFor(() => {
      expect(ml.maps).toHaveLength(1);
      return ml.maps[0];
    });
    map.fire('load');
    expect(onReady).not.toHaveBeenCalled();
    vi.advanceTimersByTime(8000);
    expect(onReady).toHaveBeenCalledExactlyOnceWith({ ok: false, reason: 'timeout' });
    map.fire('idle');
    expect(onReady).toHaveBeenCalledOnce();
  });

  it('draws nothing past the Mercator limit and never loads MapLibre for it', async () => {
    const onReady = vi.fn();
    render(ShareMap, {
      observation: exact({ lat: 86 }),
      interactive: true,
      basemap: RASTER,
      onReady,
    });
    expect(onReady).toHaveBeenCalledExactlyOnceWith({ ok: false, reason: 'not_mercator_safe' });
    expect(document.querySelector('.share-map')).toBeNull();
    await new Promise((r) => setTimeout(r, 20));
    expect(ml.maps).toHaveLength(0);
  });

  it('fetches the basemap when none is given', async () => {
    vi.mocked(api.basemapOnce).mockResolvedValue({ ...RASTER, provider: 'from-api' });
    render(ShareMap, { observation: exact(), interactive: true });
    const map = await mounted();
    expect(api.basemapOnce).toHaveBeenCalled();
    expect(map.options.style).toMatchObject({ version: 8 });
  });

  it('removes the map and its attribution control on unmount', async () => {
    const view = render(ShareMap, { observation: exact(), interactive: true, basemap: RASTER });
    const map = await mounted();
    map.fire('load');
    view.unmount();
    expect(map.removed).toBe(true);
  });
});
