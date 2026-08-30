import { describe, it, expect } from 'vitest';
import { gapKind, buildEdges, segmentTrips } from './location-path';
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

// The 2026-07-30 outbound leg from ISSUE-217, verbatim: a night training flight
// threading over the saved place LAX (750 m radius, `category: transit`) at a
// steady ~48 m/s. Eleven-second breadcrumbs, no gap in time or space.
const LAX_FLYTHROUGH: LocationPing[] = [
  ping({
    timestamp: '2026-07-30T04:27:44Z',
    lat: 33.937,
    lon: -118.3991,
    speed: 48.5,
    place: null,
  }),
  ping({
    timestamp: '2026-07-30T04:27:55Z',
    lat: 33.9409,
    lon: -118.4023,
    speed: 47.6,
    place: 'LAX',
  }),
  ping({
    timestamp: '2026-07-30T04:28:05Z',
    lat: 33.9444,
    lon: -118.4054,
    speed: 47.6,
    place: 'LAX',
  }),
  ping({
    timestamp: '2026-07-30T04:28:16Z',
    lat: 33.948,
    lon: -118.4089,
    speed: 47.9,
    place: 'LAX',
  }),
  ping({
    timestamp: '2026-07-30T04:28:27Z',
    lat: 33.9517,
    lon: -118.4125,
    speed: 47.8,
    place: null,
  }),
];

describe('gapKind — crossing a place boundary', () => {
  it('does not break the line when both endpoints were moving', () => {
    // ISSUE-217: a fly-/drive-through clips a geofence for a few pings, and
    // every place transition over 200 m was read as an unsampled gap.
    const a = LAX_FLYTHROUGH[0];
    const b = LAX_FLYTHROUGH[1];
    expect(gapKind(a, b, 528, 11)).toBeNull();
  });

  it('still breaks the line when the ping inside the place was at rest', () => {
    // The case the rule exists for: the phone sleeps through a departure and
    // the first ping after the geofence is already hundreds of metres away.
    const inside = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'Home', speed: 0 });
    const outside = ping({ timestamp: '2026-07-30T09:01:00Z', place: null, speed: 12 });
    expect(gapKind(inside, outside, 600, 60)).toBe('sparse');
  });

  it('still breaks the line when neither endpoint reports a speed', () => {
    // No speed field means no evidence of motion, so the rule keeps its
    // pre-existing behaviour rather than assuming continuity.
    const inside = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'Home', speed: null });
    const outside = ping({ timestamp: '2026-07-30T09:01:00Z', place: null, speed: null });
    expect(gapKind(inside, outside, 600, 60)).toBe('sparse');
  });

  it('still breaks the line when only the outside ping is moving', () => {
    const inside = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'Home', speed: 0.2 });
    const outside = ping({ timestamp: '2026-07-30T09:01:00Z', place: null, speed: 20 });
    expect(gapKind(inside, outside, 900, 60)).toBe('sparse');
  });

  it('still breaks the line when the silence is longer than a sampled step', () => {
    // Signal loss taken while moving — a train through a tunnel, re-appearing
    // 6 km later — is a genuine unsampled gap even though neither endpoint was
    // ever at rest. Motion alone would draw up to 42 km of invented track,
    // since the dwell rule does not catch anything under five minutes.
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'Station', speed: 8 });
    const b = ping({ timestamp: '2026-07-30T09:04:00Z', place: null, speed: 40 });
    expect(gapKind(a, b, 6000, 240)).toBe('sparse');
  });

  it('accepts a sampled step where one endpoint reports no speed', () => {
    // iOS reports -1 for "speed unavailable" while the fix is reacquiring,
    // which the receiver stores as null — and a geofence boundary is exactly
    // where that happens. The edge's own implied speed stands in.
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: null, speed: null });
    const b = ping({ timestamp: '2026-07-30T09:00:11Z', place: 'LAX', speed: 47.6 });
    expect(gapKind(a, b, 528, 11)).toBeNull();
  });

  it('keeps dashing a slow crossing where one endpoint reports no speed', () => {
    // The implied speed stands in only where it is itself evidence of motion,
    // so a wake-up ping just outside a geofence is not rescued by it.
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'Home', speed: null });
    const b = ping({ timestamp: '2026-07-30T09:00:55Z', place: null, speed: 20 });
    expect(gapKind(a, b, 220, 55)).toBe('sparse');
  });

  it('still dashes a walking pass-through', () => {
    // A deliberate limit rather than an oversight: the motion floor is ~18 km/h,
    // so a walk or jog through a saved place reads as at rest and keeps the
    // break. ISSUE-217 is about crossings at speed, and lifting the floor far
    // enough to cover a pedestrian would start suppressing real departures.
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'Park', speed: 1.4 });
    const b = ping({ timestamp: '2026-07-30T09:00:45Z', place: null, speed: 1.5 });
    expect(gapKind(a, b, 250, 45)).toBe('sparse');
  });

  it('leaves a short crossing alone regardless of motion', () => {
    // Under the boundary-skip distance the rule never fired in the first place.
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: null, speed: 30 });
    const b = ping({ timestamp: '2026-07-30T09:00:11Z', place: 'LAX', speed: 30 });
    expect(gapKind(a, b, 150, 11)).toBeNull();
  });

  it('does not let motion override a dwell-length gap', () => {
    // A long silence is a gap whatever the endpoints were doing, and that rule
    // is checked before the place-crossing one.
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'LAX', speed: 40 });
    const b = ping({ timestamp: '2026-07-30T09:10:00Z', place: null, speed: 40 });
    expect(gapKind(a, b, 20_000, 600)).toBe('sparse');
  });

  it('does not let motion override a teleport', () => {
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'LAX', speed: 40 });
    const b = ping({ timestamp: '2026-07-30T09:00:30Z', place: null, speed: 40 });
    expect(gapKind(a, b, 30_000, 30)).toBe('flight');
  });

  it('does not read a near-simultaneous cross-source jump as a flight', () => {
    // ISSUE-348: a watch track's last point and the phone's next ping, one
    // second and 171 m apart. The ratio is 171 m/s, which cleared the speed
    // threshold and drew a coral great-circle arc across a suburb. Two pings a
    // second apart are near-simultaneous observations of two sources that
    // disagree, not evidence of travel.
    const watch = ping({ timestamp: '2026-07-30T09:00:00Z', place: null, speed: 0 });
    const phone = ping({ timestamp: '2026-07-30T09:00:01Z', place: 'Home', speed: 0 });
    expect(gapKind(watch, phone, 171, 1)).not.toBe('flight');
  });

  it('keeps calling a real unsampled leg a flight', () => {
    // The control for the rule above. A genuine flight gap is minutes to
    // hours, so the duration floor never reaches it.
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'LAX', speed: 0 });
    const b = ping({ timestamp: '2026-07-30T14:00:00Z', place: 'JFK', speed: 0 });
    expect(gapKind(a, b, 3_970_000, 18_000)).toBe('flight');
  });

  it('trusts the ratio from the moment the gap is a gap', () => {
    // The floor is a boundary, so it is pinned from both sides: the same edge
    // one second under it is not a flight, and at it is.
    const a = ping({ timestamp: '2026-07-30T09:00:00Z', place: 'LAX', speed: 40 });
    const b = ping({ timestamp: '2026-07-30T09:00:29Z', place: null, speed: 40 });
    expect(gapKind(a, b, 30_000, 29)).not.toBe('flight');
    expect(gapKind(a, b, 30_000, 30)).toBe('flight');
  });
});

describe('the LAX fly-through', () => {
  it('draws as one unbroken run', () => {
    const edges = buildEdges(LAX_FLYTHROUGH);
    expect(edges).toHaveLength(4);
    expect(edges.map((e) => e.gap)).toEqual([null, null, null, null]);
  });

  it('stays one trip rather than three', () => {
    // The trip list is segmented on the same edges, so the dashed break split
    // the flight into fragments either side of the geofence.
    const trips = segmentTrips(LAX_FLYTHROUGH);
    expect(trips).toHaveLength(1);
    expect(trips[0].ping_count).toBe(5);
  });
});
