<!--
  One location share drawn as a map: the web chat card uses it interactive,
  the snapshot page the browser service captures uses it static. Every
  decision about what it shows is in `$lib/location/shareMap`; this keeps only
  the calls into MapLibre, which is imported on demand so a chat that never
  shows a map never downloads it.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import type { Map as MapLibreMap, IControl, StyleSpecification } from 'maplibre-gl';
  import { basemapOnce } from '$lib/api';
  import {
    DEFAULT_BASEMAP,
    buildRasterStyle,
    isRaster,
    styleUrlFor,
    type BasemapSpec,
    type MapTheme,
  } from '$lib/basemap';
  import { theme as appTheme } from '$lib/stores/theme';
  import {
    accuracyCirclePolygon,
    areaCellRing,
    fitOptions,
    hasUsableAccuracy,
    isMercatorSafe,
    mapAltText,
    observedLabel,
    type ShareObservation,
  } from '$lib/location/shareMap';

  type MapLibreModule = typeof import('maplibre-gl');

  export type ShareMapResult = { ok: true } | { ok: false; reason: string };

  interface Props {
    observation: ShareObservation;
    interactive: boolean;
    /** CSS sizes for the frame. */
    width?: string;
    height?: string;
    /** When absent, the user's basemap from `GET /api/map/basemap`. */
    basemap?: BasemapSpec;
    theme?: MapTheme | 'auto';
    /** Names the owner in the accessible label; the user id when absent. */
    ownerLabel?: string;
    onReady?: (result: ShareMapResult) => void;
  }

  let {
    observation,
    interactive,
    width = '100%',
    height = '100%',
    basemap,
    theme = 'auto',
    ownerLabel,
    onReady,
  }: Props = $props();

  const READY_TIMEOUT_MS = 8000;

  /* design-lint-allow-begin: MapLibre paint properties are read by WebGL and cannot resolve var(). */
  const OVERLAY_COLOR = '#2f7de1';
  const PIN_STROKE = '#ffffff';
  /* design-lint-allow-end */

  const safe = $derived(isMercatorSafe(observation.lat));
  const label = $derived(observedLabel(observation));
  const alt = $derived(mapAltText(observation, ownerLabel ?? observation.owner_user_id));

  let container = $state<HTMLDivElement>();
  let attributionHost = $state<HTMLSpanElement>();

  // The map is built once from the observation it mounted with. A share is a
  // fixed historical observation, so nothing redraws it in place.
  onMount(() => {
    let active = true;
    let settled = false;
    let map: MapLibreMap | undefined;
    let attribution: IControl | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let resizeObserver: ResizeObserver | undefined;

    function settle(result: ShareMapResult) {
      if (settled || !active) return;
      settled = true;
      clearTimeout(timer);
      onReady?.(result);
    }

    if (!safe) {
      settle({ ok: false, reason: 'not_mercator_safe' });
      return () => {
        active = false;
      };
    }

    timer = setTimeout(() => settle({ ok: false, reason: 'timeout' }), READY_TIMEOUT_MS);

    async function build() {
      let maplibregl: MapLibreModule;
      try {
        const [module] = await Promise.all([
          import('maplibre-gl'),
          import('maplibre-gl/dist/maplibre-gl.css'),
        ]);
        // The package's main entry is UMD, so the bundler may hand back the
        // library as the default export or as the namespace itself.
        maplibregl = (module as { default?: MapLibreModule }).default ?? module;
      } catch {
        settle({ ok: false, reason: 'module_failed' });
        return;
      }
      const spec = basemap ?? (await basemapOnce().catch(() => DEFAULT_BASEMAP));
      if (!active || !container) return;

      const mapTheme: MapTheme =
        theme === 'auto' ? (get(appTheme) === 'light' ? 'light' : 'dark') : theme;
      const style: StyleSpecification | string = isRaster(spec)
        ? buildRasterStyle(spec, mapTheme)
        : styleUrlFor(spec, mapTheme);
      const fit = fitOptions(observation);

      let m: MapLibreMap;
      try {
        m = new maplibregl.Map({
          container,
          style,
          bounds: fit.bounds,
          fitBoundsOptions: { padding: fit.padding, maxZoom: fit.maxZoom },
          attributionControl: false,
          interactive,
          cooperativeGestures: interactive,
          dragRotate: false,
          pitchWithRotate: false,
          touchPitch: false,
          ...(interactive
            ? {}
            : { fadeDuration: 0, canvasContextAttributes: { preserveDrawingBuffer: true } }),
        });
      } catch {
        settle({ ok: false, reason: 'webgl_unavailable' });
        return;
      }
      map = m;

      if (interactive) {
        m.touchZoomRotate.disableRotation();
        m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
      }

      // Expanded and in the footer rather than the collapsible corner button,
      // so a captured PNG carries the attribution in full.
      const control = new maplibregl.AttributionControl({ compact: false });
      attribution = control;
      attributionHost?.appendChild(control.onAdd(m));

      let loaded = false;
      m.on('error', (ev: { sourceId?: string; tile?: unknown }) => {
        if (!loaded) settle({ ok: false, reason: 'style_failed' });
        else if (ev?.tile || ev?.sourceId) settle({ ok: false, reason: 'tile_error' });
      });
      m.once('load', () => {
        loaded = true;
        addOverlay(m);
        m.once('idle', () => {
          if (m.areTilesLoaded()) settle({ ok: true });
          else settle({ ok: false, reason: 'tile_error' });
        });
      });

      resizeObserver = new ResizeObserver(() => m.resize());
      resizeObserver.observe(container);
    }

    function addOverlay(m: MapLibreMap) {
      if (observation.precision === 'area') {
        m.addSource('share-area', {
          type: 'geojson',
          data: {
            type: 'Feature',
            properties: {},
            geometry: {
              type: 'Polygon',
              coordinates: [areaCellRing(observation.lat, observation.lon)],
            },
          },
        });
        m.addLayer({
          id: 'share-area-fill',
          type: 'fill',
          source: 'share-area',
          paint: { 'fill-color': OVERLAY_COLOR, 'fill-opacity': 0.18 },
        });
        m.addLayer({
          id: 'share-area-line',
          type: 'line',
          source: 'share-area',
          paint: { 'line-color': OVERLAY_COLOR, 'line-width': 2 },
        });
        return;
      }
      if (hasUsableAccuracy(observation)) {
        const ring = accuracyCirclePolygon(
          observation.lat,
          observation.lon,
          observation.accuracy_m as number,
        );
        m.addSource('share-accuracy', {
          type: 'geojson',
          data: {
            type: 'Feature',
            properties: {},
            geometry: { type: 'Polygon', coordinates: [ring] },
          },
        });
        m.addLayer({
          id: 'share-accuracy-fill',
          type: 'fill',
          source: 'share-accuracy',
          paint: { 'fill-color': OVERLAY_COLOR, 'fill-opacity': 0.15 },
        });
        m.addLayer({
          id: 'share-accuracy-line',
          type: 'line',
          source: 'share-accuracy',
          paint: { 'line-color': OVERLAY_COLOR, 'line-width': 1.5, 'line-opacity': 0.8 },
        });
      }
      m.addSource('share-point', {
        type: 'geojson',
        data: {
          type: 'Feature',
          properties: {},
          geometry: { type: 'Point', coordinates: [observation.lon, observation.lat] },
        },
      });
      m.addLayer({
        id: 'share-pin',
        type: 'circle',
        source: 'share-point',
        paint: {
          'circle-radius': 7,
          'circle-color': OVERLAY_COLOR,
          'circle-stroke-color': PIN_STROKE,
          'circle-stroke-width': 2.5,
        },
      });
    }

    void build();

    return () => {
      active = false;
      clearTimeout(timer);
      resizeObserver?.disconnect();
      if (map) {
        attribution?.onRemove(map);
        map.remove();
      }
    };
  });
</script>

{#if safe}
  <figure class="share-map" style:width style:height>
    <div
      class="share-map-canvas"
      bind:this={container}
      role={interactive ? 'region' : 'img'}
      aria-label={alt}
    ></div>
    <figcaption class="share-map-footer">
      <span class="share-map-time">
        {#if observation.precision === 'area'}<span class="share-map-area">Approximate area</span
          >{/if}<span>{label}</span>
      </span>
      <span class="share-map-attribution" bind:this={attributionHost}></span>
    </figcaption>
  </figure>
{/if}

<style>
  .share-map {
    display: flex;
    flex-direction: column;
    margin: 0;
    overflow: hidden;
    background: var(--surface-card);
  }

  .share-map-canvas {
    flex: 1;
    min-height: 0;
  }

  .share-map-footer {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-1) var(--space-2);
    padding: var(--space-1) var(--space-2);
    font-size: var(--text-2xs);
    color: var(--text-secondary);
    background: var(--surface-overlay);
  }

  .share-map-time {
    display: flex;
    gap: var(--space-2);
  }

  .share-map-area {
    font-weight: var(--weight-semibold);
  }

  .share-map-attribution {
    color: var(--text-muted);
  }

  /* MapLibre's own attribution box, moved into the footer: drop its corner chrome. */
  .share-map-attribution :global(.maplibregl-ctrl-attrib) {
    margin: 0;
    padding: 0;
    background: none;
  }

  .share-map-attribution :global(.maplibregl-ctrl-attrib-button) {
    display: none;
  }

  .share-map-attribution :global(a) {
    color: inherit;
  }
</style>
