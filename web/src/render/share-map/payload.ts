// The share-map renderer's payload, as the Python caller sends it
// (`istota.location.share_maps`): the projected observation, the actor's
// resolved basemap, the owner's label, and the frame's viewport.
//
// The caller validates it before it leaves the daemon; this is the page's own
// check, so a malformed call draws nothing rather than half a map, and so a
// payload with no basemap is refused instead of sending the page to
// `GET /api/map/basemap`, which the render document can never reach.

import type { BasemapSpec } from '$lib/basemap';
import type { ShareObservation } from '$lib/location/shareMap';

export interface SnapshotPayload {
  observation: ShareObservation;
  basemap: BasemapSpec;
  ownerLabel: string;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isHttpUrl(value: unknown): value is string {
  return typeof value === 'string' && /^https?:\/\/[^/]/i.test(value);
}

function parseObservation(raw: unknown): ShareObservation | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (r.precision !== 'exact' && r.precision !== 'area') return null;
  if (!isFiniteNumber(r.lat) || Math.abs(r.lat) > 90) return null;
  if (!isFiniteNumber(r.lon) || Math.abs(r.lon) > 180) return null;
  if (r.accuracy_m != null && (!isFiniteNumber(r.accuracy_m) || r.accuracy_m < 0)) return null;
  if (typeof r.observed_at !== 'string' || !r.observed_at) return null;
  if (typeof r.owner_user_id !== 'string' || !r.owner_user_id) return null;
  return {
    precision: r.precision,
    lat: r.lat,
    lon: r.lon,
    accuracy_m: isFiniteNumber(r.accuracy_m) ? r.accuracy_m : null,
    observed_at: r.observed_at,
    freshness: typeof r.freshness === 'string' ? r.freshness : '',
    owner_user_id: r.owner_user_id,
  };
}

function parseBasemap(raw: unknown): BasemapSpec | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (r.kind !== 'raster' && r.kind !== 'style') return null;
  // The snapshot is always drawn light; dark is carried only to keep the spec whole.
  if (!isHttpUrl(r.light)) return null;
  if (r.dark !== undefined && r.dark !== '' && !isHttpUrl(r.dark)) return null;
  return {
    provider: typeof r.provider === 'string' ? r.provider : '',
    kind: r.kind,
    dark: isHttpUrl(r.dark) ? r.dark : r.light,
    light: r.light,
    attribution: typeof r.attribution === 'string' ? r.attribution : '',
    needs_key: false,
    fell_back: false,
    warning: '',
  };
}

/** The payload as the page draws it, or null when anything it needs is missing or out of shape. */
export function parseSnapshotPayload(raw: unknown): SnapshotPayload | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const observation = parseObservation(r.observation);
  const basemap = parseBasemap(r.basemap);
  if (!observation || !basemap) return null;
  const ownerLabel = typeof r.owner === 'string' && r.owner ? r.owner : observation.owner_user_id;
  return { observation, basemap, ownerLabel };
}
