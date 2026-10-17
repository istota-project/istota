// The decisions behind a location-share map, as pure functions over the
// projected observation. `ShareMap.svelte` keeps only the calls into MapLibre,
// which needs WebGL and so cannot run under jsdom; everything that decides
// what the map shows is here, where it can be tested.
//
// The map shows exactly what the share's text already discloses: an exact
// point with its accuracy, or the cell that two-decimal rounding covers. It
// never fits on anything finer than the values it is given.

export type SharePrecision = 'exact' | 'area';

/** The projected observation a share carries, as `location_snapshot` builds it. */
export interface ShareObservation {
  precision: SharePrecision;
  lat: number;
  lon: number;
  /** Exact only; null for an area share. */
  accuracy_m?: number | null;
  /** ISO 8601, as the backend writes it. */
  observed_at: string;
  freshness: string;
  owner_user_id: string;
}

/** `[[west, south], [east, north]]`, the order MapLibre's `fitBounds` takes. */
export type LngLatBoundsTuple = [[number, number], [number, number]];

export interface ShareFitOptions {
  bounds: LngLatBoundsTuple;
  padding: number;
  maxZoom: number;
}

/** Web Mercator's latitude limit. Past it the projection has no row to draw. */
export const MERCATOR_MAX_LAT = 85.0511;
export const EXACT_MAX_ZOOM = 15;
export const AREA_MAX_ZOOM = 12;
export const FIT_PADDING_PX = 32;
/** Half the width of the cell that rounding to two decimals maps onto one value. */
export const AREA_HALF_CELL_DEG = 0.005;

const EARTH_RADIUS_M = 6371008.8;

export function isMercatorSafe(lat: number): boolean {
  return Number.isFinite(lat) && Math.abs(lat) <= MERCATOR_MAX_LAT;
}

/**
 * The cell two-decimal rounding maps onto `(lat, lon)`.
 *
 * East is never wrapped back into [-180, 180]: a cell straddling the
 * antimeridian stays one 0.01-degree box (east may be 180.001) rather than
 * becoming a box spanning the whole world the other way round.
 */
export function areaCellBounds(lat: number, lon: number): LngLatBoundsTuple {
  return [
    [lon - AREA_HALF_CELL_DEG, lat - AREA_HALF_CELL_DEG],
    [lon + AREA_HALF_CELL_DEG, lat + AREA_HALF_CELL_DEG],
  ];
}

/** The cell as a closed GeoJSON ring. */
export function areaCellRing(lat: number, lon: number): [number, number][] {
  const [[w, s], [e, n]] = areaCellBounds(lat, lon);
  return [
    [w, s],
    [e, s],
    [e, n],
    [w, n],
    [w, s],
  ];
}

/**
 * A closed ring of `steps + 1` points `radiusM` metres from the centre, on a
 * sphere. Longitudes are offsets from the centre's and are not wrapped, so a
 * circle at the antimeridian stays contiguous.
 */
export function accuracyCirclePolygon(
  lat: number,
  lon: number,
  radiusM: number,
  steps = 64,
): [number, number][] {
  const rad = Math.PI / 180;
  const lat1 = lat * rad;
  const angular = radiusM / EARTH_RADIUS_M;
  const ring: [number, number][] = [];
  for (let i = 0; i < steps; i++) {
    const bearing = (2 * Math.PI * i) / steps;
    const lat2 = Math.asin(
      Math.sin(lat1) * Math.cos(angular) + Math.cos(lat1) * Math.sin(angular) * Math.cos(bearing),
    );
    const dLon = Math.atan2(
      Math.sin(bearing) * Math.sin(angular) * Math.cos(lat1),
      Math.cos(angular) - Math.sin(lat1) * Math.sin(lat2),
    );
    ring.push([lon + dLon / rad, lat2 / rad]);
  }
  ring.push(ring[0]);
  return ring;
}

export function hasUsableAccuracy(obs: ShareObservation): boolean {
  if (obs.precision !== 'exact') return false;
  const radius = obs.accuracy_m;
  return typeof radius === 'number' && Number.isFinite(radius) && radius > 0;
}

export function fitOptions(obs: ShareObservation): ShareFitOptions {
  if (obs.precision === 'area') {
    return {
      bounds: areaCellBounds(obs.lat, obs.lon),
      padding: FIT_PADDING_PX,
      maxZoom: AREA_MAX_ZOOM,
    };
  }
  if (!hasUsableAccuracy(obs)) {
    return {
      bounds: [
        [obs.lon, obs.lat],
        [obs.lon, obs.lat],
      ],
      padding: FIT_PADDING_PX,
      maxZoom: EXACT_MAX_ZOOM,
    };
  }
  const ring = accuracyCirclePolygon(obs.lat, obs.lon, obs.accuracy_m as number);
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  for (const [x, y] of ring) {
    west = Math.min(west, x);
    east = Math.max(east, x);
    south = Math.min(south, y);
    north = Math.max(north, y);
  }
  return {
    bounds: [
      [west, south],
      [east, north],
    ],
    padding: FIT_PADDING_PX,
    maxZoom: EXACT_MAX_ZOOM,
  };
}

/** `2026-10-16 14:02 UTC`, or null when the timestamp cannot be read. */
function utcMinute(observedAt: string): string | null {
  const ms = Date.parse(observedAt);
  if (!Number.isFinite(ms)) return null;
  const iso = new Date(ms).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/** The footer's time line. Absolute UTC, so a PNG read later still says when. */
export function observedLabel(obs: ShareObservation): string {
  const time = utcMinute(obs.observed_at);
  if (time === null) return 'Observed time unknown';
  if (obs.freshness === 'last_known') return `Last known, observed ${time}`;
  return `Observed ${time}`;
}

export function mapAltText(obs: ShareObservation, ownerLabel: string): string {
  const what = obs.precision === 'area' ? 'approximate area' : 'exact location';
  const time = utcMinute(obs.observed_at);
  const when = time === null ? 'observed at an unknown time' : `observed ${time}`;
  return `Map: ${ownerLabel}'s ${what}, ${when}`;
}
