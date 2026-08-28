// Turning the server's resolved basemap into something MapLibre can render.
//
// The location maps used to name `basemaps.cartocdn.com` as a literal in
// LocationMap.svelte. CARTO now requires an API key and watermarks keyless
// requests, so every map rendered defaced tiles and no deployment could change
// it without a code edit (ISSUE-334). The source is now config: the backend
// resolves a provider to concrete URLs (`istota/map_basemap.py`) and this
// module turns that answer into a style.
//
// It lives outside the component because the component cannot be tested —
// MapLibre needs WebGL, which jsdom does not have. Everything with a decision
// in it is here, as pure functions over plain data; the component keeps only
// the calls into MapLibre.
//
// Two shapes, and the difference drives how a theme switch works:
//
//   raster  — `dark`/`light` are tile-URL templates. Both are put in one style
//             as two layers, so switching theme is a visibility toggle and the
//             data layers on top are never touched.
//   style   — `dark`/`light` are MapLibre style URLs. A style URL replaces the
//             whole style, so the component must re-add its data layers
//             afterwards. That is the price of vector tiles and it is paid in
//             `applyBasemapTheme`, not here.

import type { StyleSpecification, RasterSourceSpecification } from 'maplibre-gl';

export type MapTheme = 'light' | 'dark';

// Stable layer ids, so nothing downstream keys on a provider name. The old
// code called these `carto-dark-layer` / `carto-light-layer`, which is the
// naming that made the vendor look load-bearing.
export const BASEMAP_DARK_LAYER = 'basemap-dark';
export const BASEMAP_LIGHT_LAYER = 'basemap-light';

/** The resolved basemap, exactly as `GET /api/map/basemap` returns it. */
export interface BasemapSpec {
  provider: string;
  kind: 'raster' | 'style';
  dark: string;
  light: string;
  attribution: string;
  needs_key: boolean;
  fell_back: boolean;
  warning: string;
}

// What the map falls back to when the config request fails. Keyless on
// purpose: a fallback that needs a credential is not a fallback. Kept in step
// with `DEFAULT_PROVIDER` in `istota/map_basemap.py` — if they drift, the only
// cost is that an offline first paint uses a different provider than the
// server would have chosen, not a broken map.
export const DEFAULT_BASEMAP: BasemapSpec = {
  provider: 'openfreemap',
  kind: 'style',
  dark: 'https://tiles.openfreemap.org/styles/dark',
  light: 'https://tiles.openfreemap.org/styles/positron',
  attribution: '',
  needs_key: false,
  fell_back: false,
  warning: '',
};

/**
 * A style whose sources are all raster.
 *
 * Narrower than `StyleSpecification` — whose `sources` is a union covering
 * GeoJSON and video — so a caller can read `tiles` and `attribution` without
 * a cast. Still assignable to `StyleSpecification` for MapLibre itself.
 */
export type RasterBasemapStyle = Omit<StyleSpecification, 'sources'> & {
  sources: Record<string, RasterSourceSpecification>;
};

export function isRaster(spec: BasemapSpec): boolean {
  return spec.kind === 'raster';
}

/** The style URL for one theme, falling back to the other rather than to nothing. */
export function styleUrlFor(spec: BasemapSpec, theme: MapTheme): string {
  const wanted = theme === 'dark' ? spec.dark : spec.light;
  return wanted || spec.light || spec.dark;
}

/**
 * A v8 style holding both raster themes, with `theme` visible.
 *
 * Both are present so `applyBasemapTheme` can switch with two
 * `setLayoutProperty` calls. Rebuilding the style instead would drop every
 * data layer the map has added on top and force them all to be re-added, for
 * a change that is two lines of paint.
 */
export function buildRasterStyle(spec: BasemapSpec, theme: MapTheme): RasterBasemapStyle {
  const sources: Record<string, RasterSourceSpecification> = {};

  // Keyed by URL so a provider serving one flavour to both themes (the OSM
  // standard layer has no dark style) gets one source and not two fetches of
  // every tile.
  const sourceIdFor = (url: string): string => {
    const existing = Object.entries(sources).find(([, s]) => s.tiles?.[0] === url);
    if (existing) return existing[0];
    const id = `basemap-src-${Object.keys(sources).length}`;
    sources[id] = {
      type: 'raster',
      tiles: [url],
      tileSize: 256,
      attribution: spec.attribution,
    };
    return id;
  };

  const darkSource = sourceIdFor(spec.dark || spec.light);
  const lightSource = sourceIdFor(spec.light || spec.dark);
  const showLight = theme === 'light';

  return {
    version: 8,
    sources,
    layers: [
      {
        id: BASEMAP_DARK_LAYER,
        type: 'raster',
        source: darkSource,
        minzoom: 0,
        maxzoom: 20,
        layout: { visibility: showLight ? 'none' : 'visible' },
      },
      {
        id: BASEMAP_LIGHT_LAYER,
        type: 'raster',
        source: lightSource,
        minzoom: 0,
        maxzoom: 20,
        layout: { visibility: showLight ? 'visible' : 'none' },
      },
    ],
  };
}
