<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import type { LocationPing, DaySummaryStop, Place } from '$lib/api';

	interface Props {
		center?: [number, number];
		zoom?: number;
		pings?: LocationPing[];
		stops?: DaySummaryStop[];
		places?: Place[];
		currentPosition?: { lat: number; lon: number } | null;
		showPath?: boolean;
		showHeat?: boolean;
		onStopClick?: (stop: DaySummaryStop) => void;
	}

	let {
		center = [-118.3, 34.1],
		zoom = 12,
		pings = [],
		stops = [],
		places = [],
		currentPosition = null,
		showPath = true,
		showHeat = false,
		onStopClick,
	}: Props = $props();

	let container: HTMLDivElement;
	let map: maplibregl.Map | undefined;
	let mapLoaded = false;
	let currentMarker: maplibregl.Marker | undefined;
	let resizeObserver: ResizeObserver | undefined;

	export function flyTo(lat: number, lon: number, z?: number) {
		map?.flyTo({ center: [lon, lat], zoom: z ?? 15, duration: 800 });
	}

	function buildPathGeoJSON(pings: LocationPing[]): GeoJSON.FeatureCollection {
		const coords = pings.map(p => [p.lon, p.lat]);
		return {
			type: 'FeatureCollection',
			features: coords.length >= 2
				? [{
					type: 'Feature',
					properties: {},
					geometry: { type: 'LineString', coordinates: coords },
				}]
				: [],
		};
	}

	function buildPingPointsGeoJSON(pings: LocationPing[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: pings.map(p => ({
				type: 'Feature' as const,
				properties: { timestamp: p.timestamp, place: p.place },
				geometry: { type: 'Point' as const, coordinates: [p.lon, p.lat] },
			})),
		};
	}

	function buildStopsGeoJSON(stops: DaySummaryStop[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: stops.map(s => ({
				type: 'Feature' as const,
				properties: {
					location: s.location,
					arrived: s.arrived,
					departed: s.departed,
					ping_count: s.ping_count,
				},
				geometry: { type: 'Point' as const, coordinates: [s.lon, s.lat] },
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
				},
				layers: [{
					id: 'carto-dark-layer',
					type: 'raster',
					source: 'carto-dark',
					minzoom: 0,
					maxzoom: 20,
				}],
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
			fitBounds();
		});
	}

	function addSources() {
		if (!map) return;
		map.addSource('path', { type: 'geojson', data: buildPathGeoJSON([]) });
		map.addSource('ping-points', { type: 'geojson', data: buildPingPointsGeoJSON([]) });
		map.addSource('stops', { type: 'geojson', data: buildStopsGeoJSON([]) });
		map.addSource('places', { type: 'geojson', data: buildPlacesGeoJSON([]) });
	}

	function addLayers() {
		if (!map) return;

		// Place radius circles
		map.addLayer({
			id: 'place-radius',
			type: 'circle',
			source: 'places',
			paint: {
				'circle-radius': [
					'interpolate', ['exponential', 2], ['zoom'],
					10, ['/', ['get', 'radius_meters'], 50],
					18, ['/', ['get', 'radius_meters'], 0.3],
				],
				'circle-color': 'rgba(51, 51, 51, 0.15)',
				'circle-stroke-color': '#555',
				'circle-stroke-width': 1,
			},
		});

		// Path trace
		map.addLayer({
			id: 'path-line',
			type: 'line',
			source: 'path',
			layout: { visibility: 'visible' },
			paint: {
				'line-color': '#4a9eff',
				'line-width': 2.5,
				'line-opacity': 0.7,
			},
		});

		// Heat map layer
		map.addLayer({
			id: 'heat',
			type: 'heatmap',
			source: 'ping-points',
			layout: { visibility: 'none' },
			paint: {
				'heatmap-weight': 1,
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

		// Stop markers
		map.addLayer({
			id: 'stop-markers',
			type: 'circle',
			source: 'stops',
			paint: {
				'circle-radius': 6,
				'circle-color': '#e0e0e0',
				'circle-stroke-color': '#111',
				'circle-stroke-width': 2,
			},
		});

		// Stop labels
		map.addLayer({
			id: 'stop-labels',
			type: 'symbol',
			source: 'stops',
			layout: {
				'text-field': ['get', 'location'],
				'text-size': 11,
				'text-offset': [0, 1.5],
				'text-anchor': 'top',
				'text-allow-overlap': false,
			},
			paint: {
				'text-color': '#ccc',
				'text-halo-color': '#111',
				'text-halo-width': 1.5,
			},
		});

		// Place labels
		map.addLayer({
			id: 'place-labels',
			type: 'symbol',
			source: 'places',
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

		// Click handler for stops
		map.on('click', 'stop-markers', (e) => {
			if (!e.features?.length || !onStopClick) return;
			const props = e.features[0].properties;
			const geom = e.features[0].geometry;
			if (geom.type === 'Point') {
				const stop: DaySummaryStop = {
					location: props.location,
					location_source: null,
					arrived: props.arrived,
					departed: props.departed,
					ping_count: props.ping_count,
					lat: geom.coordinates[1],
					lon: geom.coordinates[0],
				};
				onStopClick(stop);
			}
		});

		map.on('mouseenter', 'stop-markers', () => {
			if (map) map.getCanvas().style.cursor = 'pointer';
		});
		map.on('mouseleave', 'stop-markers', () => {
			if (map) map.getCanvas().style.cursor = '';
		});
	}

	function updateSources() {
		if (!map || !mapLoaded) return;

		const pathSrc = map.getSource('path') as maplibregl.GeoJSONSource;
		const pingSrc = map.getSource('ping-points') as maplibregl.GeoJSONSource;
		const stopSrc = map.getSource('stops') as maplibregl.GeoJSONSource;
		const placeSrc = map.getSource('places') as maplibregl.GeoJSONSource;

		if (pathSrc) pathSrc.setData(buildPathGeoJSON(pings));
		if (pingSrc) pingSrc.setData(buildPingPointsGeoJSON(pings));
		if (stopSrc) stopSrc.setData(buildStopsGeoJSON(stops));
		if (placeSrc) placeSrc.setData(buildPlacesGeoJSON(places));
	}

	function updateLayerVisibility() {
		if (!map || !mapLoaded) return;
		map.setLayoutProperty('path-line', 'visibility', showPath ? 'visible' : 'none');
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
		currentMarker = new maplibregl.Marker({ element: el })
			.setLngLat([currentPosition.lon, currentPosition.lat])
			.addTo(map);
	}

	function fitBounds() {
		if (!map || !mapLoaded) return;
		const allCoords: [number, number][] = [];
		for (const p of pings) allCoords.push([p.lon, p.lat]);
		for (const s of stops) allCoords.push([s.lon, s.lat]);
		if (currentPosition) allCoords.push([currentPosition.lon, currentPosition.lat]);

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
		map?.remove();
	});

	$effect(() => {
		pings;
		stops;
		places;
		updateSources();
		fitBounds();
	});

	$effect(() => {
		showPath;
		showHeat;
		updateLayerVisibility();
	});

	$effect(() => {
		currentPosition;
		updateCurrentMarker();
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
</style>
