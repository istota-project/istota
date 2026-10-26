// ephemeris.js against published values. Every expected time and angle below
// was published by someone else, with the exact inputs given beside it; none
// is this file's own output.
//
// Sun: NOAA Global Monitoring Laboratory, Solar Calculator, "Sunrise/Sunset/
// Solar Noon Table for 2026", https://gml.noaa.gov/grad/solcalc/table.php
// with the latitude, longitude and year given, read 2026-10-25. Times are
// local, at the UTC offset the table states for that day.
//
// Moon, and civil twilight: U.S. Naval Observatory, "Sun and Moon Data for One
// Day", https://aa.usno.navy.mil/api/rstt/oneday with the date, coordinates
// and tz given and dst=false, read 2026-10-25.
//
// Positions: Jean Meeus, Astronomical Algorithms, 2nd ed. (1998), Example
// 25.a (the Sun, 1992 October 13.0 TD) and Example 47.a (the Moon, 1992
// April 12.0 TD).

import { describe, expect, it } from 'vitest';
import { loadStarterScript } from '../../testing/classicScript';

type Crossing = { at: number; rising: boolean };
type Ephemeris = {
  DELTA_T_SECONDS: number;
  SUN_HORIZON: number;
  sun(ms: number, lat: number, lon: number): { altitude: number; ra: number; dec: number };
  moon(
    ms: number,
    lat: number,
    lon: number,
  ): {
    altitude: number;
    ra: number;
    dec: number;
    longitude: number;
    latitude: number;
    distance: number;
    illumination: number;
    waxing: boolean;
  };
  sunCrossings(from: number, to: number, lat: number, lon: number, altitude: number): Crossing[];
  moonCrossings(from: number, to: number, lat: number, lon: number): Crossing[];
  transits(body: 'sun' | 'moon', from: number, to: number, lat: number, lon: number): number[];
  sunDay(
    from: number,
    to: number,
    lat: number,
    lon: number,
  ): { rise: number | null; set: number | null; noon: number | null };
  moonDay(
    from: number,
    to: number,
    lat: number,
    lon: number,
  ): { rise: number | null; set: number | null; transit: number | null };
};

const E = loadStarterScript('light-planner', 'ephemeris.js').Ephemeris as Ephemeris;

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

type Place = {
  name: string;
  lat: number;
  lon: number;
  day: [number, number, number];
  offset: number;
  noaa: { rise: string; set: string; noon: string };
  usno: { moonrise: string; moonset: string; civilDawn: string; civilDusk: string };
};

const PLACES: Place[] = [
  {
    // NOAA: lat=40.7128&lon=-74.006&year=2026, June 21, America/New_York -4.0.
    // USNO: date=2026-06-21&coords=40.7128,-74.006&tz=-4.
    name: 'New York, June solstice',
    lat: 40.7128,
    lon: -74.006,
    day: [2026, 6, 21],
    offset: -4,
    noaa: { rise: '05:25', set: '20:31', noon: '12:57:47' },
    usno: { moonrise: '12:50', moonset: '00:29', civilDawn: '04:52', civilDusk: '21:04' },
  },
  {
    // NOAA: lat=-33.8688&lon=151.2093&year=2026, December 21, Australia/Sydney 11.0.
    // USNO: date=2026-12-21&coords=-33.8688,151.2093&tz=11.
    name: 'Sydney, December solstice',
    lat: -33.8688,
    lon: 151.2093,
    day: [2026, 12, 21],
    offset: 11,
    noaa: { rise: '05:41', set: '20:05', noon: '12:52:47' },
    usno: { moonrise: '17:06', moonset: '02:29', civilDawn: '05:11', civilDusk: '20:35' },
  },
  {
    // NOAA: lat=64.1466&lon=-21.9426&year=2026, March 20, Atlantic/Reykjavik 0.0.
    // USNO: date=2026-03-20&coords=64.1466,-21.9426&tz=0.
    name: 'Reykjavik, March equinox',
    lat: 64.1466,
    lon: -21.9426,
    day: [2026, 3, 20],
    offset: 0,
    noaa: { rise: '07:29', set: '19:43', noon: '13:35:20' },
    usno: { moonrise: '07:14', moonset: '22:58', civilDawn: '06:41', civilDusk: '20:31' },
  },
];

/** The local day's bounds, at a fixed UTC offset. */
function localDay(place: Place): [number, number] {
  const [y, m, d] = place.day;
  const from = Date.UTC(y, m - 1, d) - place.offset * HOUR;
  return [from, from + 24 * HOUR];
}

/** A local `HH:MM[:SS]` on the place's day, as a UTC instant. */
function at(place: Place, clock: string): number {
  const [h, m, s = 0] = clock.split(':').map(Number);
  return localDay(place)[0] + h * HOUR + m * MINUTE + s * 1000;
}

function minutesOff(actual: number | null | undefined, expected: number) {
  expect(actual, 'an event was found').toEqual(expect.any(Number));
  return Math.abs((actual as number) - expected) / MINUTE;
}

describe.each(PLACES)('$name', (place) => {
  const [from, to] = localDay(place);

  it("puts sunrise, sunset and solar noon within 2 minutes of NOAA's", () => {
    const day = E.sunDay(from, to, place.lat, place.lon);
    expect(minutesOff(day.rise, at(place, place.noaa.rise))).toBeLessThanOrEqual(2);
    expect(minutesOff(day.set, at(place, place.noaa.set))).toBeLessThanOrEqual(2);
    expect(minutesOff(day.noon, at(place, place.noaa.noon))).toBeLessThanOrEqual(2);
  });

  it("puts moonrise and moonset within 10 minutes of the USNO's", () => {
    const day = E.moonDay(from, to, place.lat, place.lon);
    expect(minutesOff(day.rise, at(place, place.usno.moonrise))).toBeLessThanOrEqual(10);
    expect(minutesOff(day.set, at(place, place.usno.moonset))).toBeLessThanOrEqual(10);
  });

  it("puts civil dawn and dusk, the sun at -6 degrees, within 2 minutes of the USNO's", () => {
    const crossings = E.sunCrossings(from, to, place.lat, place.lon, -6);
    const dawn = crossings.find((c) => c.rising)?.at;
    const dusk = crossings.find((c) => !c.rising)?.at;
    expect(minutesOff(dawn, at(place, place.usno.civilDawn))).toBeLessThanOrEqual(2);
    expect(minutesOff(dusk, at(place, place.usno.civilDusk))).toBeLessThanOrEqual(2);
  });
});

/** A JDE (dynamical time) as the UTC instant the ephemeris takes. */
function fromJde(jde: number) {
  return (jde - 2440587.5) * 86_400_000 - E.DELTA_T_SECONDS * 1000;
}

describe('positions', () => {
  it("matches Meeus's Example 25.a for the Sun", () => {
    // 1992 October 13.0 TD: alpha 13h13m31.4s = 198.38083 deg, delta -7 deg 47' 06" = -7.78507.
    const sun = E.sun(fromJde(2448908.5), 0, 0);
    expect(Math.abs(sun.ra - 198.38083)).toBeLessThan(0.005);
    expect(Math.abs(sun.dec - -7.78507)).toBeLessThan(0.005);
  });

  it("matches Meeus's Example 47.a for the Moon, from the series' leading terms", () => {
    // 1992 April 12.0 TD: lambda 133.162655, beta -3.229126, Delta 368409.7 km,
    // apparent alpha 134.688470, delta 13.768368.
    const moon = E.moon(fromJde(2448724.5), 0, 0);
    expect(Math.abs(moon.longitude - 133.162655)).toBeLessThan(0.02);
    expect(Math.abs(moon.latitude - -3.229126)).toBeLessThan(0.01);
    expect(Math.abs(moon.distance - 368409.7)).toBeLessThan(30);
    expect(Math.abs(moon.ra - 134.68847)).toBeLessThan(0.03);
    expect(Math.abs(moon.dec - 13.768368)).toBeLessThan(0.03);
  });

  it('gives a nearly full moon on a published full moon, and a thin one near new', () => {
    // USNO, https://aa.usno.navy.mil/api/moon/phases/date?date=2026-10-01&nump=6,
    // read 2026-10-25: full moon 2026-10-26 04:12 UT, new moon 2026-11-09 07:02 UT.
    const full = E.moon(Date.UTC(2026, 9, 26, 4, 12), 0, 0);
    expect(full.illumination).toBeGreaterThan(0.99);
    const before = E.moon(Date.UTC(2026, 9, 25), 0, 0);
    expect(before.waxing).toBe(true);
    const fresh = E.moon(Date.UTC(2026, 10, 9, 7, 2), 0, 0);
    expect(fresh.illumination).toBeLessThan(0.01);
  });
});

describe('the shape of the answers', () => {
  it('has no sunrise or sunset in a polar summer, and says the sun stays up', () => {
    // Longyearbyen, 78.22 N, on the June solstice: the sun never sets.
    const from = Date.UTC(2026, 5, 20, 22);
    const day = E.sunDay(from, from + 24 * HOUR, 78.2232, 15.6267);
    expect(day.rise).toBeNull();
    expect(day.set).toBeNull();
    expect(E.sun(from, 78.2232, 15.6267).altitude).toBeGreaterThan(0);
  });

  it('orders crossings in time and alternates them', () => {
    const [from] = localDay(PLACES[0]);
    const week = E.sunCrossings(from, from + 7 * 24 * HOUR, 40.7128, -74.006, E.SUN_HORIZON);
    expect(week).toHaveLength(14);
    for (let i = 1; i < week.length; i++) {
      expect(week[i].at).toBeGreaterThan(week[i - 1].at);
      expect(week[i].rising).toBe(!week[i - 1].rising);
    }
    expect(E.transits('sun', from, from + 7 * 24 * HOUR, 40.7128, -74.006)).toHaveLength(7);
  });
});
