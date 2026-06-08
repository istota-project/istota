<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { get } from 'svelte/store';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import type { LocationPing, Place, DiscoveredCluster, DismissedCluster } from '$lib/api';
	import { ACTIVITY_COLORS, SPEED_GRADIENT_STOPS } from '$lib/location-constants';
	import { buildEdges, filterAccuratePings, greatCircleArc } from '$lib/location-path';
	import { theme } from '$lib/stores/theme';

	interface Props {
		center?: [number, number];
		zoom?: number;
		pings?: LocationPing[];
		places?: Place[];
		clusters?: DiscoveredCluster[];
		dismissedClusters?: DismissedCluster[];
		currentPosition?: { lat: number; lon: number } | null;
		currentSource?: 'tracker' | 'browser';
		showPath?: boolean;
		showHeat?: boolean;
		activeActivityTypes?: Set<string> | null;
		selectedPlaceId?: number | null;
		pickingLocation?: boolean;
		onClusterClick?: (cluster: DiscoveredCluster) => void;
		onDismissedClusterClick?: (dismissed: DismissedCluster) => void;
		onPlaceMove?: (placeId: number, lat: number, lon: number) => void;
		onMapClick?: (lat: number, lon: number) => void;
	}

	let {
		center = [-118.3, 34.1],
		zoom = 12,
		pings = [],
		places = [],
		clusters = [],
		dismissedClusters = [],
		currentPosition = null,
		currentSource = 'tracker',
		showPath = true,
		showHeat = false,
		activeActivityTypes = null,
		selectedPlaceId = null,
		pickingLocation = false,
		onClusterClick,
		onDismissedClusterClick,
		onPlaceMove,
		onMapClick,
	}: Props = $props();

	let container: HTMLDivElement;
	let map: maplibregl.Map | undefined;
	let mapLoaded = false;
	let hasFittedBounds = false;
	let currentMarker: maplibregl.Marker | undefined;
	let dragMarker: maplibregl.Marker | undefined;
	let resizeObserver: ResizeObserver | undefined;
	let pickClickHandler: ((e: maplibregl.MapMouseEvent) => void) | null = null;

	export function flyTo(lat: number, lon: number, z?: number) {
		map?.flyTo({ center: [lon, lat], zoom: z ?? 15, duration: 800 });
	}

	function buildPathGeoJSON(pings: LocationPing[]): GeoJSON.FeatureCollection {
		const edges = buildEdges(pings, activeActivityTypes);
		if (edges.length === 0) return { type: 'FeatureCollection', features: [] };

		const features: GeoJSON.Feature[] = edges.map(e => {
			if (e.gap === 'flight') {
				return {
					type: 'Feature',
					properties: { segment_type: 'flight-gap' },
					geometry: {
						type: 'LineString',
						coordinates: greatCircleArc(e.a[0], e.a[1], e.b[0], e.b[1]),
					},
				};
			}
			if (e.gap === 'sparse') {
				return {
					type: 'Feature',
					properties: { segment_type: 'sparse-gap' },
					geometry: { type: 'LineString', coordinates: [e.a, e.b] },
				};
			}
			return {
				type: 'Feature',
				properties: {
					segment_type: 'activity',
					speed_kmh: e.speedKmh,
				},
				geometry: { type: 'LineString', coordinates: [e.a, e.b] },
			};
		});

		return { type: 'FeatureCollection', features };
	}

	// Cap per-ping dwell weight so a data-collection gap (phone off, lost signal)
	// doesn't dominate the heatmap. 300 s matches the default visit-exit threshold.
	const HEATMAP_MAX_DWELL_S = 300;

	function buildPingPointsGeoJSON(pings: LocationPing[]): GeoJSON.FeatureCollection {
		const sorted = [...pings].sort(
			(a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp),
		);
		const ts = sorted.map(p => Date.parse(p.timestamp));
		const dwellS = sorted.map((_, i) => {
			const next = i + 1 < ts.length ? (ts[i + 1] - ts[i]) / 1000 : NaN;
			const prev = i > 0 ? (ts[i] - ts[i - 1]) / 1000 : NaN;
			const gap = Number.isFinite(next) ? next : prev;
			if (!Number.isFinite(gap) || gap <= 0) return 0;
			return Math.min(gap, HEATMAP_MAX_DWELL_S);
		});
		return {
			type: 'FeatureCollection',
			features: sorted.map((p, i) => ({
				type: 'Feature' as const,
				properties: {
					timestamp: p.timestamp,
					place: p.place,
					activity_type: p.activity_type ?? 'stationary',
					dwell_s: dwellS[i],
				},
				geometry: { type: 'Point' as const, coordinates: [p.lon, p.lat] },
			})),
		};
	}

	function buildClustersGeoJSON(clusters: DiscoveredCluster[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: clusters.map(c => ({
				type: 'Feature' as const,
				properties: {
					total_pings: c.total_pings,
					first_seen: c.first_seen,
					last_seen: c.last_seen,
					radius_meters: c.radius_meters ?? 100,
				},
				geometry: { type: 'Point' as const, coordinates: [c.lon, c.lat] },
			})),
		};
	}

	function buildDismissedGeoJSON(rows: DismissedCluster[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: rows.map(d => ({
				type: 'Feature' as const,
				properties: {
					id: d.id,
					radius_meters: d.radius_meters,
					dismissed_at: d.dismissed_at,
				},
				geometry: { type: 'Point' as const, coordinates: [d.lon, d.lat] },
			})),
		};
	}

	function buildPlacesGeoJSON(places: Place[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: places.map(p => ({
				type: 'Feature' as const,
				properties: {
					name: p.name,
					category: p.category,
					radius_meters: p.radius_meters,
				},
				geometry: { type: 'Point' as const, coordinates: [p.lon, p.lat] },
			})),
		};
	}

	function metersToPixels(lat: number, meters: number, zoom: number): number {
		return meters / (78271.484 * Math.cos(lat * Math.PI / 180) / Math.pow(2, zoom));
	}

	function initMap() {
		// Both CARTO basemaps are added; visibility is toggled by theme so a
		// light/dark switch is an instant layer swap (no setStyle rebuild that
		// would drop our data layers). Initial visibility follows the saved theme.
		const startLight = get(theme) === 'light';
		map = new maplibregl.Map({
			container,
			style: {
				version: 8,
				sources: {
					'carto-dark': {
						type: 'raster',
						tiles: ['https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png'],
						tileSize: 256,
						attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
					},
					'carto-light': {
						type: 'raster',
						tiles: ['https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png'],
						tileSize: 256,
						attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
					},
				},
				layers: [
					{
						id: 'carto-dark-layer',
						type: 'raster',
						source: 'carto-dark',
						minzoom: 0,
						maxzoom: 20,
						layout: { visibility: startLight ? 'none' : 'visible' },
					},
					{
						id: 'carto-light-layer',
						type: 'raster',
						source: 'carto-light',
						minzoom: 0,
						maxzoom: 20,
						layout: { visibility: startLight ? 'visible' : 'none' },
					},
				],
			},
			center: center as [number, number],
			zoom,
			attributionControl: false,
		});

		map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
		map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

		map.on('load', () => {
			mapLoaded = true;
			addSources();
			addLayers();
			updateSources();
			updateCurrentMarker();
			applyMapTheme(get(theme));
			fitBounds();
		});
	}

	function addSources() {
		if (!map) return;
		map.addSource('path', { type: 'geojson', data: buildPathGeoJSON([]) });
		map.addSource('ping-points', { type: 'geojson', data: buildPingPointsGeoJSON([]) });
		map.addSource('places', { type: 'geojson', data: buildPlacesGeoJSON([]) });
		map.addSource('clusters', { type: 'geojson', data: buildClustersGeoJSON([]) });
		map.addSource('dismissed', { type: 'geojson', data: buildDismissedGeoJSON([]) });
	}

	function addLayers() {
		if (!map) return;

		// Place radius circles — meters to pixels via exponential zoom interpolation.
		// At zoom z, ground resolution at lat ~34°: meters/px = 78271.484 * cos(34°) / 2^z
		// Divisor = meters/px, so circle-radius = radius_meters / divisor = pixels.
		// MapLibre clamps (doesn't extrapolate) past the last stop, so stops must
		// span the full usable zoom range.
		const mPerPxBase = 78271.484 * Math.cos(34.1 * Math.PI / 180); // ~64810
		map.addLayer({
			id: 'place-radius',
			type: 'circle',
			source: 'places',
			paint: {
				'circle-radius': [
					'interpolate', ['exponential', 2], ['zoom'],
					8, ['/', ['get', 'radius_meters'], mPerPxBase / 256],      // z8
					16, ['/', ['get', 'radius_meters'], mPerPxBase / 65536],    // z16
					22, ['/', ['get', 'radius_meters'], mPerPxBase / 4194304],  // z22
				],
				'circle-color': 'rgba(51, 51, 51, 0.15)',
				'circle-stroke-color': '#555',
				'circle-stroke-width': 1,
			},
		});

		// Sparse-sampling connectors — Overland's significant-location-change
		// mode (and any time the phone misses pings for a few minutes) produces
		// big spatial gaps that are still ground travel. Quiet neutral dash so
		// they read as "we didn't see this leg" rather than "this leg was a
		// teleport." Straight 2-point line — no arc, since it's local.
		map.addLayer({
			id: 'path-gap-sparse',
			type: 'line',
			source: 'path',
			filter: ['==', ['get', 'segment_type'], 'sparse-gap'],
			layout: { visibility: 'visible', 'line-cap': 'round' },
			paint: {
				'line-color': '#7a8794',
				'line-width': 2,
				'line-opacity': 0.4,
				'line-dasharray': [2, 3],
			},
		});

		// Flight connectors — implied speed above any ground transport. Faded
		// coral great-circle arc so a transatlantic hop bows over the pole
		// instead of slicing a Mercator straight line.
		map.addLayer({
			id: 'path-gap-flight',
			type: 'line',
			source: 'path',
			filter: ['==', ['get', 'segment_type'], 'flight-gap'],
			layout: { visibility: 'visible', 'line-cap': 'round' },
			paint: {
				'line-color': '#e88a8a',
				'line-width': 2.5,
				'line-opacity': 0.45,
				'line-dasharray': [2, 2],
			},
		});

		// Speed gradient: interpolate color along a continuous km/h scale.
		const speedColorExpr: any = [
			'interpolate',
			['linear'],
			['get', 'speed_kmh'],
			...SPEED_GRADIENT_STOPS.flatMap(([kmh, color]) => [kmh, color]),
		];

		// Activity path trace — solid, speed-coloured. The speed gradient alone
		// conveys mode (walking vs cycling vs transit), so no secondary dashed
		// style is needed.
		map.addLayer({
			id: 'path-line',
			type: 'line',
			source: 'path',
			filter: ['==', ['get', 'segment_type'], 'activity'],
			layout: { visibility: 'visible' },
			paint: {
				'line-color': speedColorExpr,
				'line-width': 2.5,
				'line-opacity': 0.75,
			},
		});

		// Heat map layer
		map.addLayer({
			id: 'heat',
			type: 'heatmap',
			source: 'ping-points',
			layout: { visibility: 'none' },
			paint: {
				// Weight each ping by how long it represents (seconds → minutes),
				// so a stationary hour isn't outweighed by a fast flyby with a
				// high sampling rate. interpolate clamps the effective range so
				// extreme dwells don't flatten the colour scale.
				'heatmap-weight': [
					'interpolate', ['linear'], ['get', 'dwell_s'],
					0, 0,
					HEATMAP_MAX_DWELL_S, HEATMAP_MAX_DWELL_S / 60,
				],
				'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 10, 0.5, 16, 1.5],
				'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 10, 8, 16, 25],
				'heatmap-color': [
					'interpolate', ['linear'], ['heatmap-density'],
					0, 'rgba(0,0,0,0)',
					0.15, '#1a237e',
					0.3, '#4a148c',
					0.5, '#b71c1c',
					0.7, '#ff6f00',
					1, '#ffeb3b',
				],
				'heatmap-opacity': 0.8,
			},
		});

		// Stationary pings as dots (no lines)
		map.addLayer({
			id: 'stationary-points',
			type: 'circle',
			source: 'ping-points',
			filter: ['==', ['get', 'activity_type'], 'stationary'],
			layout: { visibility: 'visible' },
			paint: {
				'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 2, 16, 4],
				'circle-color': ACTIVITY_COLORS.stationary ?? '#666',
				'circle-opacity': 0.6,
			},
		});

		// Place labels — only at neighbourhood zoom and closer, otherwise they
		// pile up illegibly on country-scale views (Tokyo alone has dozens).
		map.addLayer({
			id: 'place-labels',
			type: 'symbol',
			source: 'places',
			minzoom: 12,
			layout: {
				'text-field': ['get', 'name'],
				'text-size': 10,
				'text-offset': [0, 1.2],
				'text-anchor': 'top',
				'text-allow-overlap': false,
			},
			paint: {
				'text-color': '#888',
				'text-halo-color': '#111',
				'text-halo-width': 1,
			},
		});

		// Discovered cluster markers
		map.addLayer({
			id: 'cluster-markers',
			type: 'circle',
			source: 'clusters',
			paint: {
				'circle-radius': 10,
				'circle-color': 'rgba(255, 193, 7, 0.3)',
				'circle-stroke-color': '#ffc107',
				'circle-stroke-width': 2,
			},
		});

		// Cluster labels (ping count)
		map.addLayer({
			id: 'cluster-labels',
			type: 'symbol',
			source: 'clusters',
			layout: {
				'text-field': ['concat', ['get', 'total_pings'], 'x'],
				'text-size': 9,
				'text-offset': [0, -1.4],
				'text-anchor': 'bottom',
				'text-allow-overlap': true,
			},
			paint: {
				'text-color': '#ffc107',
				'text-halo-color': '#111',
				'text-halo-width': 1,
			},
		});

		// Click handler for clusters (both circle and label layers)
		const handleClusterClick = (e: maplibregl.MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }) => {
			if (!e.features?.length || !onClusterClick) return;
			const props = e.features[0].properties;
			const geom = e.features[0].geometry;
			if (geom.type === 'Point') {
				onClusterClick({
					lat: geom.coordinates[1],
					lon: geom.coordinates[0],
					total_pings: props.total_pings,
					first_seen: props.first_seen,
					last_seen: props.last_seen,
				});
			}
		};
		map.on('click', 'cluster-markers', handleClusterClick);
		map.on('click', 'cluster-labels', handleClusterClick);

		map.on('mouseenter', 'cluster-markers', () => {
			if (map) map.getCanvas().style.cursor = 'pointer';
		});
		map.on('mouseleave', 'cluster-markers', () => {
			if (map) map.getCanvas().style.cursor = '';
		});
		map.on('mouseenter', 'cluster-labels', () => {
			if (map) map.getCanvas().style.cursor = 'pointer';
		});
		map.on('mouseleave', 'cluster-labels', () => {
			if (map) map.getCanvas().style.cursor = '';
		});

		// Dismissed cluster zones — gray, sized by radius, restorable on click
		map.addLayer({
			id: 'dismissed-zones',
			type: 'circle',
			source: 'dismissed',
			paint: {
				'circle-radius': [
					'interpolate', ['exponential', 2], ['zoom'],
					8, ['/', ['get', 'radius_meters'], mPerPxBase / 256],
					16, ['/', ['get', 'radius_meters'], mPerPxBase / 65536],
					22, ['/', ['get', 'radius_meters'], mPerPxBase / 4194304],
				],
				'circle-color': 'rgba(120, 120, 120, 0.10)',
				'circle-stroke-color': '#888',
				'circle-stroke-width': 1,
				'circle-stroke-opacity': 0.5,
			},
		});

		map.addLayer({
			id: 'dismissed-center',
			type: 'circle',
			source: 'dismissed',
			paint: {
				'circle-radius': 5,
				'circle-color': 'rgba(120, 120, 120, 0.4)',
				'circle-stroke-color': '#888',
				'circle-stroke-width': 1,
			},
		});

		const handleDismissedClick = (e: maplibregl.MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }) => {
			if (!e.features?.length || !onDismissedClusterClick) return;
			const props = e.features[0].properties;
			const geom = e.features[0].geometry;
			if (geom.type === 'Point') {
				onDismissedClusterClick({
					id: props.id,
					lat: geom.coordinates[1],
					lon: geom.coordinates[0],
					radius_meters: props.radius_meters,
					dismissed_at: props.dismissed_at,
				});
			}
		};
		map.on('click', 'dismissed-center', handleDismissedClick);
		map.on('click', 'dismissed-zones', handleDismissedClick);
		map.on('mouseenter', 'dismissed-center', () => {
			if (map) map.getCanvas().style.cursor = 'pointer';
		});
		map.on('mouseleave', 'dismissed-center', () => {
			if (map) map.getCanvas().style.cursor = '';
		});
	}

	function updateSources() {
		if (!map || !mapLoaded) return;

		const pathSrc = map.getSource('path') as maplibregl.GeoJSONSource;
		const pingSrc = map.getSource('ping-points') as maplibregl.GeoJSONSource;
		const placeSrc = map.getSource('places') as maplibregl.GeoJSONSource;
		const clusterSrc = map.getSource('clusters') as maplibregl.GeoJSONSource;
		const dismissedSrc = map.getSource('dismissed') as maplibregl.GeoJSONSource;

		if (pathSrc) pathSrc.setData(buildPathGeoJSON(pings));
		if (pingSrc) pingSrc.setData(buildPingPointsGeoJSON(filterAccuratePings(pings, activeActivityTypes)));
		if (placeSrc) placeSrc.setData(buildPlacesGeoJSON(places));
		if (clusterSrc) clusterSrc.setData(buildClustersGeoJSON(clusters));
		if (dismissedSrc) dismissedSrc.setData(buildDismissedGeoJSON(dismissedClusters));
	}

	// Swap basemap + recolor map-data styling for the active theme. The label
	// halos, place-radius strokes, and dismissed-zone strokes are tuned for the
	// dark basemap; light tiles need a white halo and lighter strokes.
	function applyMapTheme(t: 'light' | 'dark') {
		if (!map || !mapLoaded) return;
		const light = t === 'light';
		map.setLayoutProperty('carto-light-layer', 'visibility', light ? 'visible' : 'none');
		map.setLayoutProperty('carto-dark-layer', 'visibility', light ? 'none' : 'visible');
		const halo = light ? '#ffffff' : '#111111';
		map.setPaintProperty('place-labels', 'text-color', light ? '#444444' : '#888888');
		map.setPaintProperty('place-labels', 'text-halo-color', halo);
		map.setPaintProperty('cluster-labels', 'text-halo-color', halo);
		map.setPaintProperty('place-radius', 'circle-stroke-color', light ? '#9a9aa2' : '#555555');
		map.setPaintProperty('dismissed-zones', 'circle-stroke-color', light ? '#9a9aa2' : '#888888');
	}

	function updateLayerVisibility() {
		if (!map || !mapLoaded) return;
		map.setLayoutProperty('path-line', 'visibility', showPath ? 'visible' : 'none');
		map.setLayoutProperty('path-transit', 'visibility', showPath ? 'visible' : 'none');
		map.setLayoutProperty('path-gap-sparse', 'visibility', showPath ? 'visible' : 'none');
		map.setLayoutProperty('path-gap-flight', 'visibility', showPath ? 'visible' : 'none');
		map.setLayoutProperty('stationary-points', 'visibility', showPath ? 'visible' : 'none');
		map.setLayoutProperty('heat', 'visibility', showHeat ? 'visible' : 'none');
	}

	function updateCurrentMarker() {
		if (currentMarker) {
			currentMarker.remove();
			currentMarker = undefined;
		}
		if (!map || !currentPosition) return;

		const el = document.createElement('div');
		el.className = 'current-position-marker';
		if (currentSource === 'browser') el.classList.add('browser');
		currentMarker = new maplibregl.Marker({ element: el })
			.setLngLat([currentPosition.lon, currentPosition.lat])
			.addTo(map);
	}

	function updateDragMarker() {
		if (dragMarker) {
			dragMarker.remove();
			dragMarker = undefined;
		}
		if (!map || !selectedPlaceId || !onPlaceMove) return;

		const place = places.find(p => p.id === selectedPlaceId);
		if (!place) return;

		const el = document.createElement('div');
		el.className = 'place-drag-marker';

		dragMarker = new maplibregl.Marker({ element: el, draggable: true })
			.setLngLat([place.lon, place.lat])
			.addTo(map);

		dragMarker.on('dragend', () => {
			if (!dragMarker) return;
			const lngLat = dragMarker.getLngLat();
			onPlaceMove(place.id, lngLat.lat, lngLat.lng);
		});
	}

	function fitBounds() {
		if (!map || !mapLoaded) return;

		// Priority: activity data > clusters > places
		const activityCoords: [number, number][] = [];
		for (const p of pings) activityCoords.push([p.lon, p.lat]);
		if (currentPosition) activityCoords.push([currentPosition.lon, currentPosition.lat]);

		let allCoords = activityCoords;
		if (allCoords.length === 0) {
			for (const c of clusters) allCoords.push([c.lon, c.lat]);
		}
		if (allCoords.length === 0) {
			for (const p of places) allCoords.push([p.lon, p.lat]);
		}

		if (allCoords.length === 0) return;
		if (allCoords.length === 1) {
			map.setCenter(allCoords[0]);
			map.setZoom(14);
			return;
		}

		const bounds = new maplibregl.LngLatBounds(allCoords[0], allCoords[0]);
		for (const c of allCoords) bounds.extend(c);
		map.fitBounds(bounds, { padding: 60, maxZoom: 16, duration: 0 });
	}

	onMount(() => {
		initMap();
		resizeObserver = new ResizeObserver(() => map?.resize());
		resizeObserver.observe(container);
	});

	onDestroy(() => {
		resizeObserver?.disconnect();
		currentMarker?.remove();
		dragMarker?.remove();
		map?.remove();
	});

	$effect(() => {
		pings;
		places;
		clusters;
		dismissedClusters;
		activeActivityTypes;
		updateSources();
		if (!hasFittedBounds) {
			fitBounds();
			hasFittedBounds = true;
		}
	});

	$effect(() => {
		showPath;
		showHeat;
		updateLayerVisibility();
	});

	$effect(() => {
		applyMapTheme($theme);
	});

	$effect(() => {
		currentPosition;
		currentSource;
		updateCurrentMarker();
	});

	$effect(() => {
		selectedPlaceId;
		places;
		updateDragMarker();
	});

	$effect(() => {
		if (!map) return;
		const canvas = map.getCanvas();
		canvas.style.cursor = pickingLocation ? 'crosshair' : '';
		if (pickClickHandler) {
			map.off('click', pickClickHandler);
			pickClickHandler = null;
		}
		if (pickingLocation && onMapClick) {
			const cb = onMapClick;
			pickClickHandler = (e) => cb(e.lngLat.lat, e.lngLat.lng);
			map.on('click', pickClickHandler);
		}
	});
</script>

<div bind:this={container} class="map-container"></div>

<style>
	.map-container {
		width: 100%;
		height: 100%;
		min-height: 300px;
		border-radius: var(--radius-card);
		overflow: hidden;
	}

	/* Current position pulsing marker */
	:global(.current-position-marker) {
		width: 14px;
		height: 14px;
		background: #4aff7f;
		border: 2px solid #111;
		border-radius: 50%;
		box-shadow: 0 0 0 0 rgba(74, 255, 127, 0.4);
		animation: pulse 2s ease-out infinite;
	}

	@keyframes pulse {
		0% { box-shadow: 0 0 0 0 rgba(74, 255, 127, 0.5); }
		100% { box-shadow: 0 0 0 12px rgba(74, 255, 127, 0); }
	}

	/* Browser-geolocation fallback marker — orange to differentiate from tracker pings */
	:global(.current-position-marker.browser) {
		background: #ff9800;
		box-shadow: 0 0 0 0 rgba(255, 152, 0, 0.4);
		animation: pulse-browser 2s ease-out infinite;
	}

	@keyframes pulse-browser {
		0% { box-shadow: 0 0 0 0 rgba(255, 152, 0, 0.5); }
		100% { box-shadow: 0 0 0 12px rgba(255, 152, 0, 0); }
	}

	/* Draggable place marker */
	:global(.place-drag-marker) {
		width: 18px;
		height: 18px;
		background: #ffc107;
		border: 2px solid #111;
		border-radius: 50%;
		cursor: grab;
		box-shadow: 0 0 0 3px rgba(255, 193, 7, 0.3);
	}

	:global(.place-drag-marker:active) {
		cursor: grabbing;
	}

	/* Dark theme for MapLibre controls */
	:global(.maplibregl-ctrl-group) {
		background: #1a1a1a !important;
		border: 1px solid #333 !important;
		border-radius: 0.4rem !important;
	}

	:global(.maplibregl-ctrl-group button) {
		border-color: #333 !important;
	}

	:global(.maplibregl-ctrl-group button:not(:disabled):hover) {
		background-color: #222 !important;
	}

	:global(.maplibregl-ctrl-group button span) {
		filter: invert(0.8);
	}

	:global(.maplibregl-ctrl-attrib) {
		background: rgba(17, 17, 17, 0.7) !important;
		color: #666 !important;
		font-size: 0.65rem !important;
	}

	:global(.maplibregl-ctrl-attrib a) {
		color: #888 !important;
	}

	/* Light theme — markers keep a contrasting border against light tiles, and
	   the MapLibre controls/attribution flip to a light surface with dark icons.
	   The dark rules above are untouched. */
	:global(:root[data-theme='light'] .current-position-marker),
	:global(:root[data-theme='light'] .place-drag-marker) {
		border-color: #fff;
	}
	/* The bright tracker green washes out on light tiles — darken the dot. */
	:global(:root[data-theme='light'] .current-position-marker) {
		background: #16a34a;
	}
	:global(:root[data-theme='light'] .maplibregl-ctrl-group) {
		background: #fff !important;
		border-color: var(--border-default) !important;
	}
	:global(:root[data-theme='light'] .maplibregl-ctrl-group button) {
		border-color: #d4d4d8 !important;
	}
	:global(:root[data-theme='light'] .maplibregl-ctrl-group button:not(:disabled):hover) {
		background-color: #ececef !important;
	}
	:global(:root[data-theme='light'] .maplibregl-ctrl-group button span) {
		filter: none;
	}
	:global(:root[data-theme='light'] .maplibregl-ctrl-attrib) {
		background: rgba(255, 255, 255, 0.75) !important;
		color: #555 !important;
	}
	:global(:root[data-theme='light'] .maplibregl-ctrl-attrib a) {
		color: #2563b0 !important;
	}
</style>
