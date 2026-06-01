// Shared path-segmentation pipeline for the location map and the trip list.
//
// The map line and the "trips" panel must agree by construction: a trip is
// exactly one solid, continuous run of the map path — the consecutive pings
// the map draws as coloured activity edges, bounded by the same dwell / sparse
// gap / flight gaps that break the line visually. To guarantee that, both the
// map (buildEdges → buildPathGeoJSON in LocationMap.svelte) and segmentTrips()
// below feed off the same filtered ping array and the same edge classifier.
//
// This module used to live inside LocationMap.svelte as component-private
// functions; it was lifted out so the trip list can reuse the literal same
// code instead of a parallel (and divergent) backend segmenter.

import type { LocationPing } from './api';

// Low-accuracy pings (phone inside a building, multipath in airport terminals,
// etc.) are dropped before any path or trip computation — they produce the
// scattered spaghetti and lone ocean segments seen around airports.
export const MAX_ACCURACY_M = 100;

export function filterAccuratePings(
	pings: LocationPing[],
	activeActivityTypes?: Set<string> | null,
): LocationPing[] {
	const accurate = pings.filter(
		p => p.accuracy == null || p.accuracy <= MAX_ACCURACY_M,
	);
	if (!activeActivityTypes) return accurate;
	return accurate.filter(p => activeActivityTypes.has(p.activity_type ?? 'stationary'));
}

export function approxDistanceM(lon1: number, lat1: number, lon2: number, lat2: number): number {
	const dlat = (lat2 - lat1) * 111_000;
	const dlon = (lon2 - lon1) * 111_000 * Math.cos(((lat1 + lat2) / 2) * Math.PI / 180);
	return Math.sqrt(dlat * dlat + dlon * dlon);
}

// Great-circle arc between two lon/lat points, sampled along the shortest
// spherical path (slerp on unit vectors). Used to render long gap edges
// (flights, cross-continent jumps) as curved polylines that also take the
// Pacific shortcut when appropriate instead of going around the long way.
// Returns segments+1 points. Longitudes are unwrapped so consecutive
// deltas stay within ±180°, letting MapLibre cross the anti-meridian
// without visual wrap-around.
const EARTH_RADIUS_M = 6_371_000;
const GAP_ARC_MIN_M = 500_000;       // below this, straight line is fine
const GAP_ARC_SEGMENT_M = 200_000;   // one segment per ~200 km
const GAP_ARC_MAX_SEGMENTS = 64;

export function haversineM(lon1: number, lat1: number, lon2: number, lat2: number): number {
	const phi1 = lat1 * Math.PI / 180;
	const phi2 = lat2 * Math.PI / 180;
	const dphi = (lat2 - lat1) * Math.PI / 180;
	const dlam = (lon2 - lon1) * Math.PI / 180;
	const a = Math.sin(dphi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dlam / 2) ** 2;
	return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(a)));
}

export function greatCircleArc(
	lon1: number, lat1: number,
	lon2: number, lat2: number,
): [number, number][] {
	const distM = haversineM(lon1, lat1, lon2, lat2);
	if (distM < GAP_ARC_MIN_M) return [[lon1, lat1], [lon2, lat2]];
	const segments = Math.min(
		GAP_ARC_MAX_SEGMENTS,
		Math.max(2, Math.round(distM / GAP_ARC_SEGMENT_M)),
	);
	const phi1 = lat1 * Math.PI / 180;
	const phi2 = lat2 * Math.PI / 180;
	const lam1 = lon1 * Math.PI / 180;
	const lam2 = lon2 * Math.PI / 180;
	const d = distM / EARTH_RADIUS_M;
	const sinD = Math.sin(d);
	if (sinD === 0) return [[lon1, lat1], [lon2, lat2]];
	const points: [number, number][] = [];
	let prevLon = lon1;
	for (let i = 0; i <= segments; i++) {
		const f = i / segments;
		const A = Math.sin((1 - f) * d) / sinD;
		const B = Math.sin(f * d) / sinD;
		const x = A * Math.cos(phi1) * Math.cos(lam1) + B * Math.cos(phi2) * Math.cos(lam2);
		const y = A * Math.cos(phi1) * Math.sin(lam1) + B * Math.cos(phi2) * Math.sin(lam2);
		const z = A * Math.sin(phi1) + B * Math.sin(phi2);
		const lat = Math.atan2(z, Math.sqrt(x * x + y * y)) * 180 / Math.PI;
		let lon = Math.atan2(y, x) * 180 / Math.PI;
		if (i > 0) {
			const delta = lon - prevLon;
			if (delta > 180) lon -= 360;
			else if (delta < -180) lon += 360;
		}
		points.push([lon, lat]);
		prevLon = lon;
	}
	return points;
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
const GAP_SPEED_MAX_MS = 140;            // ~500 km/h — above any ground transport (Shinkansen ~300), below any flight
const FLIGHT_DIST_MIN_M = 300_000;       // 300 km — a single un-sampled edge of this length is flight-shaped even when airport dwell drags the implied speed below cruise
const FLIGHT_DIST_SPEED_MS = 28;         // ~100 km/h — paired with FLIGHT_DIST_MIN_M, keeps slow-and-long edges (overnight ferries, very long parked stretches) in the sparse bucket
const FLIGHT_ENDPOINT_REST_MS = 5;       // ~18 km/h — below this, OS-reported endpoint speed counts as "at rest"; tolerates walk-to-gate noise
const PLACE_CROSSING_DIST_M = 200;       // boundary-skip threshold

// 'flight' = implied speed too fast for any ground transport (clear teleport).
// 'sparse' = the gap is consistent with ground travel that just wasn't
// sampled — typical when Overland is on "significant location change"
// instead of continuous tracking. Rendering uses this to draw flights as
// curved arcs and sparse ground gaps as quiet straight dashes.
export type GapKind = 'flight' | 'sparse' | null;

// True when the OS-reported instantaneous speed says the user was clearly
// moving when this ping was sampled. Null speed (Overland didn't report)
// is treated as at-rest so we degrade to the pre-OS-speed behaviour for
// users / configs that don't carry the field.
function endpointInMotion(p: LocationPing): boolean {
	return p.speed != null && p.speed > FLIGHT_ENDPOINT_REST_MS;
}

export function gapKind(a: LocationPing, b: LocationPing, dist: number, timeDeltaS: number): GapKind {
	if (timeDeltaS <= 0) return null;
	const speed = dist / timeDeltaS;
	// Unambiguous teleport — fast enough that no ground transport could
	// have covered the edge even at cruise speed.
	if (speed > GAP_SPEED_MAX_MS) return 'flight';
	// Long jump with airport dwell on either side: a 700 km gap at 35 m/s
	// (driving + airport) is still a flight in user terms. The speed floor
	// keeps very-slow long edges (parked-overnight + moved-far-next-day)
	// in the sparse bucket. The endpoint-at-rest check handles the
	// flight-vs-rail-with-signal-loss ambiguity: a flight ends at airport
	// gates where the user is stationary; a high-speed-rail signal gap
	// leaves the user mid-journey at both boundaries, still in motion.
	// Trust the rule only when neither endpoint reports motion.
	if (dist > FLIGHT_DIST_MIN_M && speed > FLIGHT_DIST_SPEED_MS) {
		if (!endpointInMotion(a) && !endpointInMotion(b)) return 'flight';
	}
	if (timeDeltaS >= DWELL_MIN_DURATION_S) return 'sparse';
	const placeA = a.place ?? null;
	const placeB = b.place ?? null;
	if (placeA !== placeB && dist > PLACE_CROSSING_DIST_M) return 'sparse';
	return null;
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
const MAX_DISPLAY_KMH = 320;

export interface Edge {
	a: [number, number];
	b: [number, number];
	speedKmh: number;
	speedMs: number;
	dt: number;
	gap: GapKind;
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
		} else {
			// Preserve the first and last ping of every dwell run. Drops
			// the middle (no visual loss — they all sit inside a 50 m
			// cluster) but leaves anchors so the surrounding edges carry
			// the dwell's at-rest OS-speed signal. Without this, a
			// multi-leg flight loses its airport gate anchors and the
			// gap edge connects the Uber pings on either side, defeating
			// gapKind's endpoint-rest check.
			kept.push(run[0]);
			if (run.length > 1) kept.push(run[run.length - 1]);
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

// A single ping whose `place` differs from both neighbours is GPS noise
// (bounce-back into a geofence on departure, drive-by past a saved place,
// stray cell/Wi-Fi fix). Null its place so the place-crossing gap rule in
// gapKind doesn't fire — but keep the ping in the time series so its
// timestamp prevents a dwell-duration false-positive gap (ISSUE-066).
// Real visits always produce 2+ consecutive pings at the same place; a
// lone ping never does. The coords may be ~100m off (cached cell/Wi-Fi
// fix), but a small spatial wobble is preferable to a missing segment.
// Returns a clone-on-edit copy so other consumers of `pings` (e.g. the
// points layer) keep the original `place`.
function stripIsolatedPlacePings(pings: LocationPing[]): LocationPing[] {
	if (pings.length < 3) return pings;
	return pings.map((p, i) => {
		if (i === 0 || i === pings.length - 1) return p;
		if (p.place == null) return p;
		const prevPlace = pings[i - 1].place ?? null;
		const nextPlace = pings[i + 1].place ?? null;
		if (prevPlace !== p.place && nextPlace !== p.place) {
			return { ...p, place: null };
		}
		return p;
	});
}

// The full cleaning pipeline that defines the drawn path: accuracy + activity
// filter, dwell exclusion, outlier removal, isolated-place stripping. Both the
// map edges and the trip segmentation start from this exact array.
export function filterPathPings(
	pings: LocationPing[],
	activeActivityTypes?: Set<string> | null,
): LocationPing[] {
	return stripIsolatedPlacePings(
		dropOutlierPings(excludeDwellPings(filterAccuratePings(pings, activeActivityTypes))),
	);
}

function edgesFromFiltered(filtered: LocationPing[]): Edge[] {
	if (filtered.length < 2) return [];
	const edges: Edge[] = [];
	for (let i = 1; i < filtered.length; i++) {
		const a = filtered[i - 1];
		const b = filtered[i];
		const aTs = new Date(a.timestamp).getTime() / 1000;
		const bTs = new Date(b.timestamp).getTime() / 1000;
		const dt = bTs - aTs;
		// Haversine here, not equirectangular: approxDistanceM is fine for
		// city-scale edges but wraps wrong across the anti-meridian and
		// over-estimates by ~17% on transcontinental jumps, throwing off
		// gapKind's speed/distance thresholds for long flights.
		const dist = haversineM(a.lon, a.lat, b.lon, b.lat);
		const speedMs = dt > 0 ? dist / dt : 0;
		edges.push({
			a: [a.lon, a.lat],
			b: [b.lon, b.lat],
			speedKmh: Math.min(speedMs * 3.6, MAX_DISPLAY_KMH),
			speedMs,
			dt,
			gap: gapKind(a, b, dist, dt),
		});
	}
	return edges;
}

export function buildEdges(
	pings: LocationPing[],
	activeActivityTypes?: Set<string> | null,
): Edge[] {
	return edgesFromFiltered(filterPathPings(pings, activeActivityTypes));
}

// One trip = one continuous solid run of the map path (a sequence of
// `gap === null` activity edges) between stops. The boundaries are the same
// dwell / sparse / flight gaps that break the drawn line, so a trip is "the
// line between two stationary stops" by construction.
export interface Trip {
	start_time: string;
	end_time: string;
	start_lat: number;
	start_lon: number;
	end_lat: number;
	end_lon: number;
	distance_m: number;
	ping_count: number;
	activity_type: string;
	max_speed: number | null;
}

// Drop runs that carry no real movement — two pings a few metres apart over a
// few seconds is GPS wobble, not a trip worth listing. A run only needs to
// clear one of the two floors (it can be a short fast hop or a long slow walk).
const TRIP_MIN_DISTANCE_M = 100;
const TRIP_MIN_DURATION_S = 120;

function buildTrip(run: LocationPing[]): Trip {
	let distance = 0;
	for (let i = 1; i < run.length; i++) {
		distance += haversineM(run[i - 1].lon, run[i - 1].lat, run[i].lon, run[i].lat);
	}

	const activityCounts: Record<string, number> = {};
	for (const p of run) {
		const a = p.activity_type ?? 'stationary';
		activityCounts[a] = (activityCounts[a] ?? 0) + 1;
	}
	let dominant = 'unknown';
	let best = -1;
	for (const [a, n] of Object.entries(activityCounts)) {
		if (n > best) {
			best = n;
			dominant = a;
		}
	}

	let maxSpeed = 0;
	let sawSpeed = false;
	for (const p of run) {
		if (p.speed != null) {
			sawSpeed = true;
			if (p.speed > maxSpeed) maxSpeed = p.speed;
		}
	}

	const first = run[0];
	const last = run[run.length - 1];
	return {
		start_time: first.timestamp,
		end_time: last.timestamp,
		start_lat: first.lat,
		start_lon: first.lon,
		end_lat: last.lat,
		end_lon: last.lon,
		distance_m: Math.round(distance),
		ping_count: run.length,
		activity_type: dominant,
		max_speed: sawSpeed ? Math.round(maxSpeed * 10) / 10 : null,
	};
}

export function segmentTrips(
	pings: LocationPing[],
	activeActivityTypes?: Set<string> | null,
): Trip[] {
	const filtered = filterPathPings(pings, activeActivityTypes);
	if (filtered.length < 2) return [];
	const edges = edgesFromFiltered(filtered);

	const trips: Trip[] = [];
	let runStart = 0; // index into `filtered`

	const flush = (endExclusive: number) => {
		const run = filtered.slice(runStart, endExclusive);
		if (run.length < 2) return;
		const trip = buildTrip(run);
		const durS = (Date.parse(trip.end_time) - Date.parse(trip.start_time)) / 1000;
		if (trip.distance_m < TRIP_MIN_DISTANCE_M && durS < TRIP_MIN_DURATION_S) return;
		trips.push(trip);
	};

	// edges[i] connects filtered[i] and filtered[i + 1]. A gap edge ends the
	// current run at filtered[i] and starts the next at filtered[i + 1].
	for (let i = 0; i < edges.length; i++) {
		if (edges[i].gap !== null) {
			flush(i + 1);
			runStart = i + 1;
		}
	}
	flush(filtered.length);

	return trips;
}
