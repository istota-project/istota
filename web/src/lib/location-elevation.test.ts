import { describe, it, expect } from 'vitest';
import {
  elevationPoints,
  elevationRange,
  elevationSummary,
  hasMeaningfulElevation,
  buildElevationPath,
  decimate,
  MIN_ELEVATION_RANGE_M,
  MAX_PLOT_POINTS,
  PROFILE_GAP_S,
} from './location-elevation';
import { DWELL_MIN_DURATION_S, MAX_ACCURACY_M } from './location-path';
import type { LocationPing } from './api';

function ping(partial: Partial<LocationPing> & { timestamp: string }): LocationPing {
  return {
    lat: 33.9,
    lon: -118.4,
    altitude: null,
    accuracy: 10,
    place: null,
    speed: null,
    battery: null,
    activity_type: 'driving',
    ...partial,
  };
}

// The 2026-07-30 climb out of Long Beach from ISSUE-218: eleven-second
// breadcrumbs, altitude in metres, climbing through pattern altitude.
const CLIMB: LocationPing[] = [
  ping({ timestamp: '2026-07-30T04:14:00Z', altitude: 335.3 }),
  ping({ timestamp: '2026-07-30T04:14:11Z', altitude: 512.1 }),
  ping({ timestamp: '2026-07-30T04:14:22Z', altitude: 741.0 }),
  ping({ timestamp: '2026-07-30T04:14:33Z', altitude: 1066.8 }),
  ping({ timestamp: '2026-07-30T04:14:44Z', altitude: 1432.6 }),
];

function xs(path: string): number[] {
  return [...path.matchAll(/[ML]([\d.]+)\s/g)].map((m) => Number(m[1]));
}

function ys(path: string): number[] {
  return [...path.matchAll(/[ML][\d.]+\s([\d.]+)/g)].map((m) => Number(m[1]));
}

describe('elevationPoints', () => {
  it('keeps only pings carrying a vertical fix', () => {
    const points = elevationPoints([
      ping({ timestamp: '2026-07-30T04:14:00Z', altitude: 335.3 }),
      ping({ timestamp: '2026-07-30T04:14:11Z', altitude: null }),
      ping({ timestamp: '2026-07-30T04:14:22Z', altitude: 741.0 }),
    ]);

    expect(points.map((p) => p.alt)).toEqual([335.3, 741.0]);
  });

  it('sorts by time, so a descending history renders left-to-right', () => {
    const points = elevationPoints([...CLIMB].reverse());

    expect(points.map((p) => p.alt)).toEqual([335.3, 512.1, 741.0, 1066.8, 1432.6]);
  });

  it('drops a ping whose timestamp will not parse', () => {
    const points = elevationPoints([
      ping({ timestamp: 'not-a-date', altitude: 500 }),
      ping({ timestamp: '2026-07-30T04:14:00Z', altitude: 335.3 }),
    ]);

    expect(points).toHaveLength(1);
    expect(points[0].alt).toBe(335.3);
  });

  it('returns nothing for pings that never carried an altitude', () => {
    expect(elevationPoints([ping({ timestamp: '2026-07-30T04:14:00Z' })])).toEqual([]);
  });

  it('drops a fix the map itself refuses to draw', () => {
    // A multipath fix indoors clears the 75 m range floor on noise alone, so
    // the strip has to apply the same accuracy gate the map line does — or it
    // invents an 860 m spike over a day spent at sea level.
    const flatDayPlusJunk = [
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 39 }),
      ping({ timestamp: '2026-07-30T04:01:00Z', altitude: 41 }),
      ping({ timestamp: '2026-07-30T04:02:00Z', altitude: 900, accuracy: MAX_ACCURACY_M + 1 }),
      ping({ timestamp: '2026-07-30T04:03:00Z', altitude: 44 }),
    ];

    expect(elevationPoints(flatDayPlusJunk).map((p) => p.alt)).toEqual([39, 41, 44]);
  });

  it('drops a lone sample that jumps away from both neighbours and back', () => {
    // iOS flags an invalid vertical fix with a negative verticalAccuracy, which
    // nothing here stores — so it arrives as a plausible-looking 0. One of those
    // on a Denver day sets the whole scale and flattens the real track.
    const denverWithDropout = [
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 1600 }),
      ping({ timestamp: '2026-07-30T04:00:11Z', altitude: 1601 }),
      ping({ timestamp: '2026-07-30T04:00:22Z', altitude: 0 }),
      ping({ timestamp: '2026-07-30T04:00:33Z', altitude: 1602 }),
      ping({ timestamp: '2026-07-30T04:00:44Z', altitude: 1603 }),
    ];

    expect(elevationPoints(denverWithDropout).map((p) => p.alt)).toEqual([1600, 1601, 1602, 1603]);
  });

  it('keeps a steep but consistent climb, which never reverses across a sample', () => {
    expect(elevationPoints(CLIMB)).toHaveLength(5);
  });

  it('keeps a large step between sparse samples, where the implied rate is low', () => {
    const sparseClimb = [
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 100 }),
      ping({ timestamp: '2026-07-30T04:10:00Z', altitude: 900 }),
      ping({ timestamp: '2026-07-30T04:20:00Z', altitude: 150 }),
    ];

    expect(elevationPoints(sparseClimb)).toHaveLength(3);
  });
});

describe('elevationRange', () => {
  it('reports the min and max of the run', () => {
    expect(elevationRange(elevationPoints(CLIMB))).toEqual({ min: 335.3, max: 1432.6 });
  });

  it('is null for an empty run, so a caller cannot divide by an absent range', () => {
    expect(elevationRange([])).toBeNull();
  });
});

describe('hasMeaningfulElevation', () => {
  it('accepts a real climb', () => {
    expect(hasMeaningfulElevation(elevationPoints(CLIMB))).toBe(true);
  });

  it('rejects a day sitting still, where the spread is GPS vertical noise', () => {
    const jitter = [0, 12, -9, 21, -4, 15].map((d, i) =>
      ping({ timestamp: `2026-07-30T0${i}:00:00Z`, altitude: 60 + d }),
    );

    expect(hasMeaningfulElevation(elevationPoints(jitter))).toBe(false);
  });

  it('rejects a run with too few vertical fixes to be a profile', () => {
    const two = elevationPoints([
      ping({ timestamp: '2026-07-30T04:14:00Z', altitude: 0 }),
      ping({ timestamp: '2026-07-30T04:14:11Z', altitude: 5000 }),
    ]);

    expect(hasMeaningfulElevation(two)).toBe(false);
  });

  it('rejects no pings at all', () => {
    expect(hasMeaningfulElevation([])).toBe(false);
  });

  it('accepts a run sitting exactly on the range floor', () => {
    const points = elevationPoints(
      [0, 1, 2, 3].map((i) =>
        ping({
          timestamp: `2026-07-30T04:1${i}:00Z`,
          altitude: 100 + (i === 3 ? MIN_ELEVATION_RANGE_M : 0),
        }),
      ),
    );

    expect(hasMeaningfulElevation(points)).toBe(true);
  });
});

describe('decimate', () => {
  it('leaves a short run alone', () => {
    const points = elevationPoints(CLIMB);

    expect(decimate(points)).toBe(points);
  });

  it('thins a long run to the cap', () => {
    const many = elevationPoints(
      Array.from({ length: 5000 }, (_, i) =>
        ping({
          timestamp: new Date(Date.UTC(2026, 6, 30, 0, 0, i)).toISOString(),
          altitude: 100 + (i % 300),
        }),
      ),
    );

    expect(many.length).toBeGreaterThan(MAX_PLOT_POINTS);
    expect(decimate(many).length).toBeLessThanOrEqual(MAX_PLOT_POINTS + 2);
  });

  it('keeps the extremes, which set the scale and the readout beside it', () => {
    const many = elevationPoints(
      Array.from({ length: 3000 }, (_, i) =>
        ping({
          timestamp: new Date(Date.UTC(2026, 6, 30, 0, 0, i)).toISOString(),
          // A single peak at an index a plain stride would step over.
          altitude: i === 1493 ? 4000 : 100 + (i % 50),
        }),
      ),
    );
    const thinned = decimate(many, 100);

    expect(elevationRange(thinned)).toEqual(elevationRange(many));
  });

  it('keeps the run in time order', () => {
    const many = elevationPoints(
      Array.from({ length: 2000 }, (_, i) =>
        ping({
          timestamp: new Date(Date.UTC(2026, 6, 30, 0, 0, i)).toISOString(),
          altitude: i === 777 ? 9000 : 100 + (i % 40),
        }),
      ),
    );
    const times = decimate(many, 50).map((p) => p.t);

    expect([...times].sort((a, b) => a - b)).toEqual(times);
  });
});

describe('buildElevationPath', () => {
  it('spans the full box, oldest at the left edge and newest at the right', () => {
    const { solid } = buildElevationPath(elevationPoints(CLIMB), 100, 40);

    expect(xs(solid)[0]).toBeCloseTo(0);
    expect(xs(solid).at(-1)).toBeCloseTo(100);
  });

  it('puts the highest point at the top of the box and the lowest at the bottom', () => {
    const { solid } = buildElevationPath(elevationPoints(CLIMB), 100, 40);
    const y = ys(solid);

    // SVG y grows downward, and both ends are inset by half a stroke so the
    // viewport clip does not shave them.
    expect(y[0]).toBeGreaterThan(38);
    expect(y[0]).toBeLessThan(40);
    expect(y.at(-1)).toBeGreaterThan(0);
    expect(y.at(-1)).toBeLessThan(2);
  });

  it('places x by time rather than by index, so a pause reads as a pause', () => {
    const uneven = elevationPoints([
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 100 }),
      ping({ timestamp: '2026-07-30T04:45:00Z', altitude: 300 }),
      ping({ timestamp: '2026-07-30T05:00:00Z', altitude: 500 }),
    ]);
    const { sparse } = buildElevationPath(uneven, 100, 40);

    expect(xs(sparse)[1]).toBeCloseTo(75);
  });

  it('draws an unsampled gap as a connector rather than as track', () => {
    const split = elevationPoints([
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 100 }),
      ping({ timestamp: '2026-07-30T04:00:11Z', altitude: 120 }),
      // Hours of silence: whatever happened in between is not ours to draw solid.
      ping({ timestamp: '2026-07-30T08:00:00Z', altitude: 900 }),
      ping({ timestamp: '2026-07-30T08:00:11Z', altitude: 920 }),
    ]);
    const { solid, sparse } = buildElevationPath(split, 100, 40);

    expect(solid.match(/M/g)).toHaveLength(2);
    expect(sparse.match(/M/g)).toHaveLength(1);
  });

  it('breaks exactly where the map dashes its own line', () => {
    expect(PROFILE_GAP_S).toBe(DWELL_MIN_DURATION_S);
  });

  it('keeps a step of exactly the gap threshold in the solid run', () => {
    const onTheLine = elevationPoints([
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 100 }),
      ping({ timestamp: '2026-07-30T04:05:00Z', altitude: 300 }),
      ping({ timestamp: '2026-07-30T04:10:00Z', altitude: 500 }),
    ]);
    const { solid, sparse } = buildElevationPath(onTheLine, 100, 40);

    expect(solid.match(/L/g)).toHaveLength(2);
    expect(sparse).toBe('');
  });

  it('moves a step one second past the threshold into the gap path', () => {
    const justOver = elevationPoints([
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 100 }),
      ping({ timestamp: '2026-07-30T04:05:01Z', altitude: 300 }),
      ping({ timestamp: '2026-07-30T04:10:01Z', altitude: 500 }),
    ]);
    const { solid, sparse } = buildElevationPath(justOver, 100, 40);

    expect(sparse.match(/M/g)).toHaveLength(1);
    expect(solid.match(/L/g)).toHaveLength(1);
  });

  it('never leaves a run drawn as an isolated moveto, which strokes nothing', () => {
    // Every sample six minutes apart — the shipped "Places" tracker profile and
    // stock Overland significant-change mode both look like this. Segmenting on
    // gaps alone would render a labelled, captioned, empty box.
    const sparseDay = elevationPoints(
      Array.from({ length: 8 }, (_, i) =>
        ping({
          timestamp: new Date(Date.UTC(2026, 6, 30, 4, i * 6)).toISOString(),
          altitude: 100 + i * 130,
        }),
      ),
    );
    const { solid, sparse } = buildElevationPath(sparseDay, 100, 40);

    expect(solid).toBe('');
    expect(sparse.match(/L/g)).toHaveLength(7);
  });

  it('keeps one run together across an ordinary sampling interval', () => {
    const { solid, sparse } = buildElevationPath(elevationPoints(CLIMB), 100, 40);

    expect(solid.match(/M/g)).toHaveLength(1);
    expect(sparse).toBe('');
  });

  it('draws a flat run down the middle rather than dividing by a zero range', () => {
    const flat = elevationPoints(
      [0, 1, 2].map((i) => ping({ timestamp: `2026-07-30T04:0${i}:00Z`, altitude: 250 })),
    );
    const { solid } = buildElevationPath(flat, 100, 40);

    expect(ys(solid).every((y) => Number.isFinite(y) && Math.abs(y - 20) < 0.001)).toBe(true);
  });

  it('centres a run collapsed onto one instant rather than pinning it left', () => {
    const sameInstant = elevationPoints([
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 100 }),
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 300 }),
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 500 }),
    ]);
    const { solid } = buildElevationPath(sameInstant, 100, 40);

    expect(xs(solid).every((x) => x === 50)).toBe(true);
  });

  it('is empty for no points, so the caller renders nothing rather than a stray dot', () => {
    expect(buildElevationPath([], 100, 40)).toEqual({ solid: '', sparse: '' });
  });
});

describe('elevationSummary', () => {
  it('reports the gate and the range from one pass over the pings', () => {
    const summary = elevationSummary(CLIMB);

    expect(summary.show).toBe(true);
    expect(summary.range).toEqual({ min: 335.3, max: 1432.6 });
    expect(summary.points).toEqual(elevationPoints(CLIMB));
  });

  it('withholds the range when the day is GPS noise, so a caller cannot show one', () => {
    const jitter = [0, 12, -9, 21, -4, 15].map((d, i) =>
      ping({ timestamp: `2026-07-30T0${i}:00:00Z`, altitude: 60 + d }),
    );
    const summary = elevationSummary(jitter);

    expect(summary.show).toBe(false);
    expect(summary.range).toBeNull();
  });

  it('handles a day with no vertical fixes at all', () => {
    const summary = elevationSummary([
      ping({ timestamp: '2026-07-30T04:00:00Z' }),
      ping({ timestamp: '2026-07-30T04:01:00Z' }),
    ]);

    expect(summary.show).toBe(false);
    expect(summary.range).toBeNull();
    expect(summary.points).toEqual([]);
  });

  it('dashes across a nulled home interval rather than inventing a level', () => {
    // ISSUE-229's load-bearing consequence: with the sentinel gone the home
    // stint is simply unsampled, so the strip breaks its line there the way
    // the map does — the alternative, carrying the last reading forward, would
    // draw a flat line asserting a measurement nobody took.
    const day = [
      ping({ timestamp: '2026-07-30T04:00:00Z', altitude: 40 }),
      ping({ timestamp: '2026-07-30T04:00:30Z', altitude: 300 }),
      ping({ timestamp: '2026-07-30T04:01:00Z', altitude: 620 }),
      // Home: declared points, arriving with no altitude at all.
      ...[0, 1, 2, 3].map((i) => ping({ timestamp: `2026-07-30T0${5 + i}:00:00Z` })),
      ping({ timestamp: '2026-07-30T12:00:00Z', altitude: 610 }),
      ping({ timestamp: '2026-07-30T12:00:30Z', altitude: 280 }),
      ping({ timestamp: '2026-07-30T12:01:00Z', altitude: 35 }),
    ];
    const { solid, sparse } = buildElevationPath(elevationSummary(day).points, 100, 40);

    // One dashed connector across the gap, and the two sampled runs either
    // side of it drawn solid.
    expect(sparse.match(/M/g)).toHaveLength(1);
    expect(solid.match(/M/g)).toHaveLength(2);
  });

  it('draws nothing for a day that never left the neighbourhood', () => {
    // ISSUE-229: the home plateau used to arrive as a run of -1s, and its
    // ~100 m spread against the real terrain passed the gate on a fabricated
    // constant. With the sentinel nulled at ingest the day is its own modest
    // spread and draws nothing, which is the signal the gate exists to give.
    const neighbourhood = [0, 1, 2, 3, 4, 5].map((i) =>
      ping({ timestamp: `2026-07-30T1${i}:00:00Z`, altitude: 100 + i * 8 }),
    );

    expect(elevationSummary(neighbourhood).show).toBe(false);
  });
});
