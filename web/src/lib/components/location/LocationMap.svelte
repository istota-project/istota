<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import type { LocationPing, Place, DiscoveredCluster } from '$lib/api';
	import { ACTIVITY_COLORS, SPEED_GRADIENT_STOPS } from '$lib/location-constants';

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
	let hasFittedBounds = false;
	let currentMarker: maplibregl.Marker | undefined;
	let dragMarker: maplibregl.Marker | undefined;
	let resizeObserver: ResizeObserver | undefined;

	export function flyTo(lat: number, lon: number, z?: number) {
		map?.flyTo({ center: [lon, lat], zoom: z ?? 15, duration: 800 });
	}

	// Low-accuracy pings (phone inside a building, multipath in airport terminals, etc.)
	// are dropped before any path or point rendering — they produce the scattered
	// spaghetti and lone ocean segments seen around LAX.
	const MAX_ACCURACY_M = 100;

	function filteredPings(pings: LocationPing[]): LocationPing[] {
		const accurate = pings.filter(
			p => p.accuracy == null || p.accuracy <= MAX_ACCURACY_M,
		);
		if (!activeActivityTypes) return accurate;
		return accurate.filter(p => activeActivityTypes!.has(p.activity_type ?? 'stationary'));
	}

	function approxDistanceM(lon1: number, lat1: number, lon2: number, lat2: number): number {
		const dlat = (lat2 - lat1) * 111_000;
		const dlon = (lon2 - lon1) * 111_000 * Math.cos(((lat1 + lat2) / 2) * Math.PI / 180);
		return Math.sqrt(dlat * dlat + dlon * dlon);
	}

	// Gap detection. An edge is a gap if any of these hold:
	//   - implied speed > 200 km/h (clear teleport)
	//   - dt >= dwell minimum (a filtered dwell sits between the two kept pings)
	//   - the two pings are in different place states (null vs place, or two
	//     different places) AND the distance crosses a place boundary. This
	//     catches "rays" — the phone waking up mid-trip after leaving a
	//     geofence, where the first new ping is already hundreds of metres
	//     away. Without this, such edges render as solid coloured straight
	//     lines cutting across blocks.
	const GAP_SPEED_MAX_MS = 55;             // ~200 km/h — clearly a teleport
	const PLACE_CROSSING_DIST_M = 200;       // boundary-skip threshold

	function isGap(a: LocationPing, b: LocationPing, dist: number, timeDeltaS: number): boolean {
		if (timeDeltaS <= 0) return false;
		if (dist / timeDeltaS > GAP_SPEED_MAX_MS) return true;
		if (timeDeltaS >= DWELL_MIN_DURATION_S) return true;
		const placeA = a.place ?? null;
		const placeB = b.place ?? null;
		return placeA !== placeB && dist > PLACE_CROSSING_DIST_M;
	}

	// Dwell detection on consecutive stationary pings: a real dwell is a long run
	// of stationary pings tightly clustered in space. Those get excluded from the
	// path. Short runs (station stops, traffic lights) or widely-spread runs
	// (mislabelled-stationary while moving, e.g. on a train) stay in the path so
	// the line follows the actual track.
	const DWELL_MIN_DURATION_S = 300;   // 5 min
	const DWELL_MAX_SPREAD_M = 50;      // tight cluster

	// Outlier detection on rogue GPS fixes (iOS multipath, urban canyons,
	// intermittent subway reacquisition). A ping B is an outlier when removing
	// it barely changes the track: it either detours far (path inflation) or
	// sits well off the line its neighbours define (perpendicular offset).
	// Lookahead lets the test see past 1-2 consecutive bad fixes by trying the
	// next few pings as C candidates — a chain of outliers can't all be
	// justified by their immediate neighbours, but one of the next good pings
	// will expose them.
	const OUTLIER_MIN_DIST_M = 100;     // AB jitter floor — below this, always keep
	const OUTLIER_PATH_RATIO = 3;       // drop if (AB + BC) > ratio * AC
	const OUTLIER_LOOKAHEAD = 3;        // try up to N next pings as C candidates
	const OUTLIER_MIN_PERP_M = 150;     // perpendicular offset floor (normal noise)
	const OUTLIER_PERP_RATIO = 0.3;     // perp threshold scales with AC length

	// Speed clamp for color gradient (km/h). Anything above maps to the top-of-scale color.
	const MAX_DISPLAY_KMH = 180;

	interface Edge {
		a: [number, number];
		b: [number, number];
		speedKmh: number;
		speedMs: number;
		dt: number;
		gap: boolean;
	}

	function isDwellRun(run: LocationPing[]): boolean {
		if (run.length < 2) return false;
		const t0 = new Date(run[0].timestamp).getTime() / 1000;
		const t1 = new Date(run[run.length - 1].timestamp).getTime() / 1000;
		if (t1 - t0 < DWELL_MIN_DURATION_S) return false;
		let sumLat = 0;
		let sumLon = 0;
		for (const p of run) {
			sumLat += p.lat;
			sumLon += p.lon;
		}
		const clat = sumLat / run.length;
		const clon = sumLon / run.length;
		for (const p of run) {
			if (approxDistanceM(clon, clat, p.lon, p.lat) > DWELL_MAX_SPREAD_M) return false;
		}
		return true;
	}

	function excludeDwellPings(pings: LocationPing[]): LocationPing[] {
		const kept: LocationPing[] = [];
		let i = 0;
		while (i < pings.length) {
			const stationary = (pings[i].activity_type ?? 'stationary') === 'stationary';
			if (!stationary) {
				kept.push(pings[i]);
				i++;
				continue;
			}
			let j = i;
			while (j < pings.length && (pings[j].activity_type ?? 'stationary') === 'stationary') j++;
			const run = pings.slice(i, j);
			if (!isDwellRun(run)) {
				for (const p of run) kept.push(p);
			}
			i = j;
		}
		return kept;
	}

	function perpDistanceToLineM(
		plat: number, plon: number,
		alat: number, alon: number,
		clat: number, clon: number,
	): number {
		const mPerDegLat = 111_000;
		const mPerDegLon = 111_000 * Math.cos(((alat + clat) / 2) * Math.PI / 180);
		const cx = (clon - alon) * mPerDegLon;
		const cy = (clat - alat) * mPerDegLat;
		const px = (plon - alon) * mPerDegLon;
		const py = (plat - alat) * mPerDegLat;
		const lenSq = cx * cx + cy * cy;
		if (lenSq === 0) return Math.sqrt(px * px + py * py);
		return Math.abs(px * cy - py * cx) / Math.sqrt(lenSq);
	}

	function isOutlierStep(a: LocationPing, b: LocationPing, c: LocationPing, ab: number): boolean {
		const ac = approxDistanceM(a.lon, a.lat, c.lon, c.lat);
		if (ac <= 0) return false;
		const bc = approxDistanceM(b.lon, b.lat, c.lon, c.lat);
		if (ab + bc > OUTLIER_PATH_RATIO * ac) return true;
		const perp = perpDistanceToLineM(b.lat, b.lon, a.lat, a.lon, c.lat, c.lon);
		const perpThreshold = Math.max(OUTLIER_MIN_PERP_M, OUTLIER_PERP_RATIO * ac);
		return perp > perpThreshold;
	}

	function dropOutlierPings(pings: LocationPing[]): LocationPing[] {
		let current = pings;
		for (let pass = 0; pass < 3; pass++) {
			if (current.length < 3) return current;
			const kept: LocationPing[] = [current[0]];
			let removed = 0;
			for (let i = 1; i < current.length - 1; i++) {
				const a = kept[kept.length - 1];
				const b = current[i];
				const ab = approxDistanceM(a.lon, a.lat, b.lon, b.lat);
				if (ab <= OUTLIER_MIN_DIST_M) {
					kept.push(b);
					continue;
				}
				let drop = false;
				for (let k = 1; k <= OUTLIER_LOOKAHEAD && i + k < current.length; k++) {
					if (isOutlierStep(a, b, current[i + k], ab)) {
						drop = true;
						break;
					}
				}
				if (drop) {
					removed++;
					continue;
				}
				kept.push(b);
			}
			kept.push(current[current.length - 1]);
			if (removed === 0) return kept;
			current = kept;
		}
		return current;
	}

	function buildEdges(pings: LocationPing[]): Edge[] {
		const filtered = dropOutlierPings(excludeDwellPings(filteredPings(pings)));
		if (filtered.length < 2) return [];

		const edges: Edge[] = [];
		for (let i = 1; i < filtered.length; i++) {
			const a = filtered[i - 1];
			const b = filtered[i];
			const aTs = new Date(a.timestamp).getTime() / 1000;
			const bTs = new Date(b.timestamp).getTime() / 1000;
			const dt = bTs - aTs;
			const dist = approxDistanceM(a.lon, a.lat, b.lon, b.lat);
			const speedMs = dt > 0 ? dist / dt : 0;
			edges.push({
				a: [a.lon, a.lat],
				b: [b.lon, b.lat],
				speedKmh: Math.min(speedMs * 3.6, MAX_DISPLAY_KMH),
				speedMs,
				dt,
				gap: isGap(a, b, dist, dt),
			});
		}
		return edges;
	}

	function buildPathGeoJSON(pings: LocationPing[]): GeoJSON.FeatureCollection {
		const edges = buildEdges(pings);
		if (edges.length === 0) return { type: 'FeatureCollection', features: [] };

		const features: GeoJSON.Feature[] = edges.map(e => {
			if (e.gap) {
				return {
					type: 'Feature',
					properties: { segment_type: 'gap' },
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

	function buildPingPointsGeoJSON(pings: LocationPing[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: pings.map(p => ({
				type: 'Feature' as const,
				properties: {
					timestamp: p.timestamp,
					place: p.place,
					activity_type: p.activity_type ?? 'stationary',
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

		// Long-jump / teleport connectors (faint dashed grey)
		map.addLayer({
			id: 'path-gap',
			type: 'line',
			source: 'path',
			filter: ['==', ['get', 'segment_type'], 'gap'],
			layout: { visibility: 'visible' },
			paint: {
				'line-color': '#555',
				'line-width': 1,
				'line-opacity': 0.3,
				'line-dasharray': [4, 4],
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
		map.setLayoutProperty('path-gap', 'visibility', showPath ? 'visible' : 'none');
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
