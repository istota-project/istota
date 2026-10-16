/**
 * A location share under an answer: an interactive map of the shared point or
 * area, a line naming whose it is, and the map link. When the map cannot be
 * drawn the card collapses to the line and the link, with no empty frame.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor } from '@testing-library/svelte';
import type { ApiDouble } from '$lib/test/apiDouble';
import type { LocationShareCardData } from '$lib/stores/segments';

// MapLibre needs WebGL, which jsdom lacks, so it is replaced at the module
// boundary, the way ShareMap's own tests do.
const ml = vi.hoisted(() => {
  type Handler = (ev?: unknown) => void;
  const maps: FakeMap[] = [];
  class FakeMap {
    options: Record<string, unknown>;
    handlers = new Map<string, Handler[]>();
    touchZoomRotate = { disableRotation() {} };
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
  class NavigationControl {}
  class AttributionControl {
    onAdd() {
      return document.createElement('div');
    }
    onRemove() {}
  }
  return {
    maps,
    throwOnConstruct: false,
    module: { default: { Map: FakeMap, NavigationControl, AttributionControl } },
  };
});

vi.mock('maplibre-gl', () => ml.module);

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const { fillApiDouble } = await import('$lib/test/apiDouble');
await fillApiDouble(api);

import LocationShareCard from './LocationShareCard.svelte';

function share(over: Partial<LocationShareCardData['observation']> = {}): LocationShareCardData {
  return {
    cardId: 4,
    ownerLabel: 'Bob',
    mapUrl: 'https://www.openstreetmap.org/?mlat=52.52&mlon=13.405#map=15/52.52/13.405',
    observation: {
      precision: 'exact',
      lat: 52.52,
      lon: 13.405,
      accuracy_m: 40,
      observed_at: '2026-10-16T14:02:31+00:00',
      freshness: 'fresh',
      owner_user_id: 'bob',
      ...over,
    },
  };
}

const frame = () => document.querySelector('.share-map');
const link = () => screen.getByRole('link', { name: 'Open map' });

beforeEach(() => {
  ml.maps.length = 0;
  ml.throwOnConstruct = false;
  vi.mocked(api.basemapOnce).mockResolvedValue({
    provider: 'osm',
    kind: 'raster',
    dark: 'https://tiles.example.test/{z}/{x}/{y}.png',
    light: 'https://tiles.example.test/{z}/{x}/{y}.png',
    attribution: 'OSM contributors',
    needs_key: false,
    fell_back: false,
    warning: '',
  });
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(cleanup);

describe('LocationShareCard', () => {
  it('shows an interactive map, the owner and precision, and the map link', async () => {
    const { container } = render(LocationShareCard, { share: share() });
    const card = container.querySelector<HTMLElement>('[data-testid="location-share-card"]');
    expect(card?.textContent).toContain('Bob');
    expect(card?.textContent).toContain('Exact location');
    expect(link().getAttribute('href')).toBe(share().mapUrl);
    expect(link().getAttribute('rel')).toContain('noopener');
    expect(frame()).not.toBeNull();
    await waitFor(() => expect(ml.maps).toHaveLength(1));
    expect(ml.maps[0].options.interactive).toBe(true);
    expect(screen.getByLabelText("Map: Bob's exact location, observed 2026-10-16 14:02 UTC")).toBe(
      container.querySelector('.share-map-canvas'),
    );
  });

  it('says approximate area for an area share', () => {
    const { container } = render(LocationShareCard, {
      share: share({ precision: 'area', lat: 52.52, lon: 13.41, accuracy_m: null }),
    });
    expect(container.querySelector('.location-share-head')?.textContent).toContain(
      'Approximate area',
    );
  });

  it('collapses to its line and link when the map cannot be drawn', async () => {
    ml.throwOnConstruct = true;
    const { container } = render(LocationShareCard, { share: share() });
    await waitFor(() => expect(frame()).toBeNull());
    expect(container.textContent).toContain('Bob');
    expect(link().getAttribute('href')).toBe(share().mapUrl);
  });

  it('collapses past the Mercator limit and never builds a map', async () => {
    render(LocationShareCard, { share: share({ lat: 86 }) });
    expect(frame()).toBeNull();
    expect(link()).toBeTruthy();
    await new Promise((r) => setTimeout(r, 20));
    expect(ml.maps).toHaveLength(0);
  });

  it('offers no link for a map URL that is not http(s)', () => {
    const bad = { ...share(), mapUrl: 'javascript:alert(1)' };
    const { container } = render(LocationShareCard, { share: bad });
    expect(container.querySelector('a')).toBeNull();
  });
});
