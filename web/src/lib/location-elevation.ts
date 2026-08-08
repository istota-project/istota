// Elevation profile for the location track view (ISSUE-218).
//
// `location_pings.altitude` has been populated on ~95% of pings all along;
// nothing downstream read it, so a flight rendered as a flat 2-D track. This
// module turns a day's pings into the strip drawn under the map.
//
// What the number is depends on where the ping came from, and no consumer can
// tell: iOS reports altitude relative to mean sea level, a Garmin watch reports
// its own (often barometric) elevation, and the datum is not stored. It is good
// enough to show a climb and a level-off, and is not an altimeter — which is
// what the strip's caption says.

import { filterAccuratePings, DWELL_MIN_DURATION_S } from './location-path';
import type { LocationPing } from './api';

export interface ElevationPoint {
  /** Epoch milliseconds. */
  t: number;
  /** Metres, as reported by the device. */
  alt: number;
}

/** Two paths over the same run: sampled steps, and the gaps between them. */
export interface ElevationPath {
  /** Continuously sampled runs — drawn solid. */
  solid: string;
  /** Connectors across an unsampled gap — drawn dashed, like the map's line. */
  sparse: string;
}

// Below this spread the strip would be drawing GPS vertical noise as terrain.
// Vertical error runs ~1.5-3x the horizontal error, so a phone sitting still
// wanders a few tens of metres; a real excursion (a drive over a pass, a climb
// to pattern altitude) clears this comfortably.
export const MIN_ELEVATION_RANGE_M = 75;

// Two points are a line segment, not a profile.
export const MIN_ELEVATION_POINTS = 3;

// A step longer than this was not sampled, it was inferred. The map dashes its
// own line at exactly this threshold (`DWELL_MIN_DURATION_S`), and the strip is
// read directly beneath it — a solid climb under a dashed track would assert
// data nobody recorded. The two must agree, hence the shared constant.
export const PROFILE_GAP_S = DWELL_MIN_DURATION_S;

// No aircraft a phone rides in sustains this. Used only to recognise a
// single-sample spike between two neighbours that agree with each other; a
// genuine climb never reverses direction across one sample.
const MAX_VERTICAL_RATE_MS = 50;

// The plot is stretched to a few hundred CSS pixels, so past roughly this many
// points every additional one lands inside a pixel already drawn — while still
// costing its own path data. A day at the range view's 50,000-ping cap
// otherwise builds a path attribute of several hundred kilobytes.
export const MAX_PLOT_POINTS = 1200;

// Half a stroke, in box units, so the highest and lowest points are not shaved
// by the SVG viewport clip.
const PLOT_INSET = 1;

/**
 * The pings that carry a usable vertical fix, oldest first.
 *
 * Low-accuracy pings go first, by the same rule and the same threshold the map
 * and the trip segmenter use (`filterAccuratePings`) — a fix the map refuses to
 * draw must not set this strip's vertical scale. That gate is what makes
 * `MIN_ELEVATION_RANGE_M` mean anything: a multipath fix inside a building
 * clears a 75 m floor on noise alone.
 *
 * Roughly 5% of remaining pings have a horizontal fix and no vertical one;
 * those are dropped rather than treated as sea level. History arrives
 * newest-first from the CLI and oldest-first from the web query, so this sorts
 * either way.
 */
export function elevationPoints(pings: LocationPing[]): ElevationPoint[] {
  const points: ElevationPoint[] = [];
  for (const p of filterAccuratePings(pings)) {
    if (p.altitude == null || !Number.isFinite(p.altitude)) continue;
    const t = Date.parse(p.timestamp);
    if (!Number.isFinite(t)) continue;
    points.push({ t, alt: p.altitude });
  }
  points.sort((a, b) => a.t - b.t);
  return dropAltitudeSpikes(points);
}

/**
 * Drop a lone sample that jumps away from both neighbours and back.
 *
 * A device with a good horizontal fix can still report a bad altitude. The one
 * case the device itself declares — iOS's negative `verticalAccuracy` — is now
 * caught at ingest (ISSUE-229), which is where it belongs; what is left here is
 * everything nobody flagged: a bad altitude reported with a plausible accuracy,
 * and any source that reports no vertical accuracy at all. One such sample sets
 * the whole scale and flattens the real day against an edge. A genuine climb,
 * however steep, does not reverse across a single sample, which is what
 * separates the two.
 */
function dropAltitudeSpikes(points: ElevationPoint[]): ElevationPoint[] {
  if (points.length < 3) return points;

  const kept: ElevationPoint[] = [points[0]];
  for (let i = 1; i < points.length - 1; i++) {
    const previous = points[i - 1];
    const current = points[i];
    const next = points[i + 1];

    const inRate = verticalRate(previous, current);
    const outRate = verticalRate(current, next);
    const reverses = inRate * outRate < 0;

    if (
      reverses &&
      Math.abs(inRate) > MAX_VERTICAL_RATE_MS &&
      Math.abs(outRate) > MAX_VERTICAL_RATE_MS
    ) {
      continue;
    }
    kept.push(current);
  }
  kept.push(points[points.length - 1]);
  return kept;
}

function verticalRate(a: ElevationPoint, b: ElevationPoint): number {
  const seconds = (b.t - a.t) / 1000;
  if (seconds <= 0) return 0;
  return (b.alt - a.alt) / seconds;
}

/** Min and max of a run, or null when there is nothing to bound. */
export function elevationRange(points: ElevationPoint[]): { min: number; max: number } | null {
  if (points.length === 0) return null;
  let min = points[0].alt;
  let max = points[0].alt;
  for (const p of points) {
    if (p.alt < min) min = p.alt;
    if (p.alt > max) max = p.alt;
  }
  return { min, max };
}

/** Whether a run is worth drawing a profile for at all. */
export function hasMeaningfulElevation(points: ElevationPoint[]): boolean {
  if (points.length < MIN_ELEVATION_POINTS) return false;
  const range = elevationRange(points);
  if (!range) return false;
  return range.max - range.min >= MIN_ELEVATION_RANGE_M;
}

/** Everything a page and the strip both need, from one pass over the pings. */
export interface ElevationSummary {
  points: ElevationPoint[];
  /** Whether there is a profile here at all — the strip's own render gate. */
  show: boolean;
  /** Null whenever `show` is false, so nothing can print a range it should
   *  not be showing. */
  range: { min: number; max: number } | null;
}

/**
 * The strip used to take raw pings and decide for itself, which was right
 * while it was the only thing that cared. Now that it lives behind the details
 * disclosure the page needs the same answer twice more — to know whether the
 * toggle has anything to offer, and for the figure in the stats bar that
 * reports the day has a profile without opening it. A day at the range view's
 * 50,000-ping cap is not something to scan three times.
 */
export function elevationSummary(pings: LocationPing[]): ElevationSummary {
  const points = elevationPoints(pings);
  const show = hasMeaningfulElevation(points);
  return { points, show, range: show ? elevationRange(points) : null };
}

/**
 * Thin a long run down to something a few hundred pixels can show.
 *
 * Strided, so the shape is preserved, but the extremes are pinned: they set the
 * vertical scale and the readout beside it, and a plot that never reaches its
 * own stated maximum reads as a bug.
 */
export function decimate(points: ElevationPoint[], limit = MAX_PLOT_POINTS): ElevationPoint[] {
  if (points.length <= limit) return points;

  const range = elevationRange(points);
  const keep = new Set<number>([0, points.length - 1]);
  const stride = (points.length - 1) / (limit - 1);
  for (let i = 0; i < limit; i++) keep.add(Math.round(i * stride));
  if (range) {
    keep.add(points.findIndex((p) => p.alt === range.min));
    keep.add(points.findIndex((p) => p.alt === range.max));
  }

  return [...keep].sort((a, b) => a - b).map((i) => points[i]);
}

function trim(n: number): string {
  return String(Number(n.toFixed(2)));
}

/**
 * SVG paths for the profile, in a `width` x `height` box.
 *
 * x is laid out by **time**, not by index, so an hour parked reads as an hour
 * rather than as one step; y is the altitude scaled to the run's own range, so
 * the strip always uses its full height. A step longer than `PROFILE_GAP_S`
 * goes into `sparse` instead of `solid` — connected, because the map connects
 * it too, but visibly not sampled.
 */
export function buildElevationPath(
  points: ElevationPoint[],
  width: number,
  height: number,
): ElevationPath {
  const empty: ElevationPath = { solid: '', sparse: '' };
  if (points.length === 0) return empty;

  const range = elevationRange(points);
  if (!range) return empty;

  const tSpan = points[points.length - 1].t - points[0].t;
  const altSpan = range.max - range.min;
  const top = PLOT_INSET;
  const bottom = height - PLOT_INSET;

  // A run collapsed onto one instant has no time axis to lay out against;
  // centre it rather than pinning it to the left edge.
  const x = (t: number) => (tSpan > 0 ? ((t - points[0].t) / tSpan) * width : width / 2);
  // A dead-flat run has no range to scale against; draw it down the middle
  // rather than dividing by zero.
  const y = (alt: number) =>
    altSpan > 0 ? bottom - ((alt - range.min) / altSpan) * (bottom - top) : height / 2;

  const at = (p: ElevationPoint) => `${trim(x(p.t))} ${trim(y(p.alt))}`;

  const solid: string[] = [];
  const sparse: string[] = [];
  let runOpen = false;
  for (let i = 1; i < points.length; i++) {
    const previous = points[i - 1];
    const current = points[i];
    const sampled = (current.t - previous.t) / 1000 <= PROFILE_GAP_S;

    if (!sampled) {
      sparse.push(`M${at(previous)} L${at(current)}`);
      runOpen = false;
      continue;
    }
    if (!runOpen) {
      solid.push(`M${at(previous)}`);
      runOpen = true;
    }
    solid.push(`L${at(current)}`);
  }

  return { solid: solid.join(' '), sparse: sparse.join(' ') };
}
