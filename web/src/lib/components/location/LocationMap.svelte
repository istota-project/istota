<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import type { LocationPing, Place, DiscoveredCluster } from '$lib/api';
	import { ACTIVITY_COLORS, DEFAULT_PATH_COLOR } from '$lib/location-constants';

	interface Props {
		center?: [number, number];
		zoom?: number;
		pings?: LocationPing[];
		places?: Place[];
		clusters?: DiscoveredCluster[];
		currentPosition?: { lat: number; lon: number } | null;
		showPath?: boolean;
		showHeat?: boolean;
		activeActivityTypes?: Set<string> | null;
		selectedPlaceId?: number | null;
		onClusterClick?: (cluster: DiscoveredCluster) => void;
		onPlaceMove?: (placeId: number, lat: number, lon: number) => void;
	}

	let {
		center = [-118.3, 34.1],
		zoom = 12,
		pings = [],
		places = [],
		clusters = [],
		currentPosition = null,
		showPath = true,
		showHeat = false,
		activeActivityTypes = null,
		selectedPlaceId = null,
		onClusterClick,
		onPlaceMove,
	}: Props = $props();

	let container: HTMLDivElement;
	let map: maplibregl.Map | undefined;
	let mapLoaded = false;
	let currentMarker: maplibregl.Marker | undefined;
	let dragMarker: maplibregl.Marker | undefined;
	let resizeObserver: ResizeObserver | undefined;

	export function flyTo(lat: number, lon: number, z?: number) {
		map?.flyTo({ center: [lon, lat], zoom: z ?? 15, duration: 800 });
	}

	function filteredPings(pings: LocationPing[]): LocationPing[] {
		if (!activeActivityTypes) return pings;
		return pings.filter(p => activeActivityTypes!.has(p.activity_type ?? 'stationary'));
	}

	function approxDistanceM(lon1: number, lat1: number, lon2: number, lat2: number): number {
		const dlat = (lat2 - lat1) * 111_000;
		const dlon = (lon2 - lon1) * 111_000 * Math.cos(((lat1 + lat2) / 2) * Math.PI / 180);
		return Math.sqrt(dlat * dlat + dlon * dlon);
	}

	// Gap detection: only break when both time and distance suggest a real location jump.
	// Short time gaps are always contiguous (normal driving can be 1km+ between pings).
	// Long time gaps need significant distance to count as a real move.
	const GAP_TIME_MIN_S = 300;     // 5 min — below this, always contiguous
	const GAP_DIST_MIN_M = 500;     // must also be this far apart
	const GAP_SPEED_MAX_MS = 55;    // ~200 km/h — above this, clearly a teleport

	function isGap(dist: number, timeDeltaS: number): boolean {
		if (timeDeltaS <= 0) return false;
		if (dist / timeDeltaS > GAP_SPEED_MAX_MS) return true;
		if (timeDeltaS < GAP_TIME_MIN_S) return false;
		return dist > GAP_DIST_MIN_M;
	}

	function buildPathGeoJSON(pings: LocationPing[]): GeoJSON.FeatureCollection {
		const filtered = filteredPings(pings);
		if (filtered.length < 2) return { type: 'FeatureCollection', features: [] };

		const features: GeoJSON.Feature[] = [];
		let segCoords: [number, number][] = [[filtered[0].lon, filtered[0].lat]];
		let segActivity = filtered[0].activity_type ?? 'unknown';
		let segLastTs = new Date(filtered[0].timestamp).getTime() / 1000;

		function flushSegment() {
			if (segCoords.length >= 2) {
				features.push({
					type: 'Feature',
					properties: { activity_type: segActivity, segment_type: 'activity' },
					geometry: { type: 'LineString', coordinates: [...segCoords] },
				});
			}
		}

		for (let i = 1; i < filtered.length; i++) {
			const activity = filtered[i].activity_type ?? 'unknown';
			const prev = segCoords[segCoords.length - 1];
			const cur: [number, number] = [filtered[i].lon, filtered[i].lat];
			const curTs = new Date(filtered[i].timestamp).getTime() / 1000;
			const dist = approxDistanceM(prev[0], prev[1], cur[0], cur[1]);
			const timeDelta = curTs - segLastTs;

			const gap = isGap(dist, timeDelta);

			if (gap) {
				// Real location jump — break with transit connector
				flushSegment();
				if (segCoords.length > 0) {
					features.push({
						type: 'Feature',
						properties: { activity_type: 'transit', segment_type: 'transit' },
						geometry: { type: 'LineString', coordinates: [prev, cur] },
					});
				}
				segCoords = [cur];
				segActivity = activity;
			} else if (activity !== segActivity) {
				// Activity change but no gap — flush and overlap so lines connect
				flushSegment();
				segCoords = [prev, cur];
				segActivity = activity;
			} else {
				segCoords.push(cur);
			}
			segLastTs = curTs;
		}
		flushSegment();

		return { type: 'FeatureCollection', features };
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

	function buildClustersGeoJSON(clusters: DiscoveredCluster[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: clusters.map(c => ({
				type: 'Feature' as const,
				properties: {
					total_pings: c.total_pings,
					first_seen: c.first_seen,
					last_seen: c.last_seen,
				},
				geometry: { type: 'Point' as const, coordinates: [c.lon, c.lat] },
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
		map.addSource('places', { type: 'geojson', data: buildPlacesGeoJSON([]) });
		map.addSource('clusters', { type: 'geojson', data: buildClustersGeoJSON([]) });
	}

	function addLayers() {
		if (!map) return;

		// Place radius circles — meters to pixels via exponential zoom interpolation.
		// Ground resolution at lat ~34°: 78271.484 * cos(34°) / 2^z meters/pixel.
		// With exponential base 2, pixels double per zoom, matching the map projection.
		const pxPerMeterAtZ15 = Math.pow(2, 15) / (78271.484 * Math.cos(34.1 * Math.PI / 180));
		map.addLayer({
			id: 'place-radius',
			type: 'circle',
			source: 'places',
			paint: {
				'circle-radius': [
					'interpolate', ['exponential', 2], ['zoom'],
					10, ['*', ['get', 'radius_meters'], pxPerMeterAtZ15 / 32],
					15, ['*', ['get', 'radius_meters'], pxPerMeterAtZ15],
				],
				'circle-color': 'rgba(51, 51, 51, 0.15)',
				'circle-stroke-color': '#555',
				'circle-stroke-width': 1,
			},
		});

		// Transit connectors (faint dashed)
		map.addLayer({
			id: 'path-transit',
			type: 'line',
			source: 'path',
			filter: ['==', ['get', 'segment_type'], 'transit'],
			layout: { visibility: 'visible' },
			paint: {
				'line-color': '#555',
				'line-width': 1,
				'line-opacity': 0.3,
				'line-dasharray': [4, 4],
			},
		});

		// Activity path trace (colored by activity type)
		map.addLayer({
			id: 'path-line',
			type: 'line',
			source: 'path',
			filter: ['==', ['get', 'segment_type'], 'activity'],
			layout: { visibility: 'visible' },
			paint: {
				'line-color': [
					'match', ['get', 'activity_type'],
					...Object.entries(ACTIVITY_COLORS).flat(),
					DEFAULT_PATH_COLOR,
				] as any,
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
				'circle-stroke-dasharray': [2, 2],
			} as any,
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

	}

	function updateSources() {
		if (!map || !mapLoaded) return;

		const pathSrc = map.getSource('path') as maplibregl.GeoJSONSource;
		const pingSrc = map.getSource('ping-points') as maplibregl.GeoJSONSource;
		const placeSrc = map.getSource('places') as maplibregl.GeoJSONSource;
		const clusterSrc = map.getSource('clusters') as maplibregl.GeoJSONSource;

		if (pathSrc) pathSrc.setData(buildPathGeoJSON(pings));
		if (pingSrc) pingSrc.setData(buildPingPointsGeoJSON(filteredPings(pings)));
		if (placeSrc) placeSrc.setData(buildPlacesGeoJSON(places));
		if (clusterSrc) clusterSrc.setData(buildClustersGeoJSON(clusters));
	}

	function updateLayerVisibility() {
		if (!map || !mapLoaded) return;
		map.setLayoutProperty('path-line', 'visibility', showPath ? 'visible' : 'none');
		map.setLayoutProperty('path-transit', 'visibility', showPath ? 'visible' : 'none');
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
		activeActivityTypes;
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

	$effect(() => {
		selectedPlaceId;
		places;
		updateDragMarker();
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
</style>
