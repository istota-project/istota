import { describe, expect, it } from 'vitest';
import {
  AREA_MAX_ZOOM,
  EXACT_MAX_ZOOM,
  accuracyCirclePolygon,
  areaCellBounds,
  fitOptions,
  hasUsableAccuracy,
  isMercatorSafe,
  mapAltText,
  observedLabel,
  type ShareObservation,
} from './shareMap';

function exact(overrides: Partial<ShareObservation> = {}): ShareObservation {
  return {
    precision: 'exact',
    lat: 52.52,
    lon: 13.405,
    accuracy_m: 40,
    observed_at: '2026-10-16T14:02:31.123456+00:00',
    freshness: 'fresh',
    owner_user_id: 'bob',
    ...overrides,
  };
}

function area(overrides: Partial<ShareObservation> = {}): ShareObservation {
  return exact({ precision: 'area', lat: 52.52, lon: 13.41, accuracy_m: null, ...overrides });
}

// Haversine, kept independent of the module's destination-point formula.
function distanceM(a: [number, number], b: [number, number]): number {
  const r = 6371008.8;
  const rad = Math.PI / 180;
  const dLat = (b[1] - a[1]) * rad;
  const dLon = (b[0] - a[0]) * rad;
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(h));
}

describe('isMercatorSafe', () => {
  it('accepts latitudes Web Mercator can draw, edges included', () => {
    expect(isMercatorSafe(0)).toBe(true);
    expect(isMercatorSafe(85.0511)).toBe(true);
    expect(isMercatorSafe(-85.0511)).toBe(true);
  });

  it('refuses latitudes past the projection and non-finite values', () => {
    expect(isMercatorSafe(85.06)).toBe(false);
    expect(isMercatorSafe(-89.9)).toBe(false);
    expect(isMercatorSafe(Number.NaN)).toBe(false);
    expect(isMercatorSafe(Number.POSITIVE_INFINITY)).toBe(false);
  });
});

describe('areaCellBounds', () => {
  it('covers the two-decimal rounding cell around the rounded point', () => {
    const [[w, s], [e, n]] = areaCellBounds(52.52, 13.41);
    expect(w).toBeCloseTo(13.405, 9);
    expect(e).toBeCloseTo(13.415, 9);
    expect(s).toBeCloseTo(52.515, 9);
    expect(n).toBeCloseTo(52.525, 9);
  });

  it.each([179.996, -179.996, 180, -180])(
    'stays one contiguous 0.01-degree cell at the antimeridian (lon %s)',
    (lon) => {
      const [[w], [e]] = areaCellBounds(10, lon);
      expect(e).toBeGreaterThan(w);
      expect(e - w).toBeCloseTo(0.01, 9);
      expect((w + e) / 2).toBeCloseTo(lon, 9);
    },
  );
});

describe('accuracyCirclePolygon', () => {
  it('is a closed ring of steps + 1 points at the given radius', () => {
    const ring = accuracyCirclePolygon(52.52, 13.405, 250, 32);
    expect(ring).toHaveLength(33);
    expect(ring[0]).toEqual(ring[32]);
    for (const point of ring) {
      expect(distanceM([13.405, 52.52], point)).toBeCloseTo(250, 0);
    }
  });

  it('defaults to 64 steps', () => {
    expect(accuracyCirclePolygon(0, 0, 10)).toHaveLength(65);
  });

  it('does not wrap longitudes across the antimeridian', () => {
    const ring = accuracyCirclePolygon(0, 179.999, 1000);
    const lons = ring.map(([lon]) => lon);
    expect(Math.max(...lons) - Math.min(...lons)).toBeLessThan(0.1);
  });
});

describe('hasUsableAccuracy', () => {
  it('needs a finite positive radius on an exact observation', () => {
    expect(hasUsableAccuracy(exact())).toBe(true);
    expect(hasUsableAccuracy(exact({ accuracy_m: 0 }))).toBe(false);
    expect(hasUsableAccuracy(exact({ accuracy_m: null }))).toBe(false);
    expect(hasUsableAccuracy(exact({ accuracy_m: Number.NaN }))).toBe(false);
    expect(hasUsableAccuracy(area({ accuracy_m: 50 }))).toBe(false);
  });
});

describe('fitOptions', () => {
  it('fits an exact observation on its accuracy circle, capped at zoom 15', () => {
    const fit = fitOptions(exact({ accuracy_m: 500 }));
    expect(fit.maxZoom).toBe(EXACT_MAX_ZOOM);
    expect(EXACT_MAX_ZOOM).toBe(15);
    const [[w, s], [e, n]] = fit.bounds;
    expect(distanceM([w, 52.52], [13.405, 52.52])).toBeCloseTo(500, -1);
    expect(distanceM([13.405, n], [13.405, 52.52])).toBeCloseTo(500, -1);
    expect(e).toBeGreaterThan(13.405);
    expect(s).toBeLessThan(52.52);
    expect(fit.padding).toBeGreaterThan(0);
  });

  it('fits an exact observation with no usable accuracy on the point itself', () => {
    const fit = fitOptions(exact({ accuracy_m: null }));
    expect(fit.bounds).toEqual([
      [13.405, 52.52],
      [13.405, 52.52],
    ]);
    expect(fit.maxZoom).toBe(15);
  });

  it('fits an area observation on its cell only, capped at zoom 12', () => {
    const fit = fitOptions(area({ accuracy_m: 5 }));
    expect(fit.maxZoom).toBe(AREA_MAX_ZOOM);
    expect(AREA_MAX_ZOOM).toBe(12);
    expect(fit.bounds).toEqual(areaCellBounds(52.52, 13.41));
  });
});

describe('observedLabel', () => {
  it('gives the absolute UTC time to the minute', () => {
    expect(observedLabel(exact())).toBe('Observed 2026-10-16 14:02 UTC');
  });

  it('converts an offset timestamp to UTC', () => {
    expect(observedLabel(exact({ observed_at: '2026-10-16T16:02:00+02:00' }))).toBe(
      'Observed 2026-10-16 14:02 UTC',
    );
  });

  it('says last known when the observation is not fresh', () => {
    expect(observedLabel(exact({ freshness: 'last_known' }))).toBe(
      'Last known, observed 2026-10-16 14:02 UTC',
    );
  });

  it('says so when the time cannot be read', () => {
    expect(observedLabel(exact({ observed_at: 'not a time' }))).toBe('Observed time unknown');
  });
});

describe('mapAltText', () => {
  it('names the owner, the precision and the time', () => {
    expect(mapAltText(exact(), 'Bob')).toBe(
      "Map: Bob's exact location, observed 2026-10-16 14:02 UTC",
    );
    expect(mapAltText(area(), 'Bob')).toBe(
      "Map: Bob's approximate area, observed 2026-10-16 14:02 UTC",
    );
  });

  it('falls back when the time cannot be read', () => {
    expect(mapAltText(area({ observed_at: '' }), 'Bob')).toBe(
      "Map: Bob's approximate area, observed at an unknown time",
    );
  });
});
