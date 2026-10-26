import { describe, expect, it } from 'vitest';
import { loadStarterScript } from '../../testing/classicScript';

type Span = { from: number; to: number };
type Window = Span & { kind: 'golden' | 'blue'; part: 'morning' | 'evening' };
type Place = { id: string; name: string; lat: number; lon: number };
type Day = Span & { key: string };
type Opportunity = Window & { place: Place; free: Span[] };
type Model = {
  GOLDEN: [number, number];
  BLUE: [number, number];
  MIN_FREE_MS: number;
  parsePlaces(result: unknown): Place[];
  localDays(now: number, count: number): Day[];
  sunBands(from: number, to: number, lat: number, lon: number, lo: number, hi: number): Span[];
  lightWindows(day: Span, place: Place): Window[];
  busyIntervals(events: unknown): Span[];
  freeParts(window: Span, busy: Span[], minimum?: number): Span[];
  freeGoldenHours(days: Day[], places: Place[], busy: Span[], now: number): Opportunity[];
  dayDetails(
    day: Span,
    place: Place,
  ): {
    sunrise: number | null;
    sunset: number | null;
    noon: number | null;
    moonrise: number | null;
    moonset: number | null;
    illumination: number;
    waxing: boolean;
    windows: Window[];
  };
  altitudeSeries(
    day: Span,
    place: Place,
    stepMinutes: number,
  ): { sun: Array<{ x: number; y: number }>; moon: Array<{ x: number; y: number }> };
};
type Ephemeris = {
  sunCrossings(
    from: number,
    to: number,
    lat: number,
    lon: number,
    altitude: number,
  ): Array<{ at: number; rising: boolean }>;
};

function load() {
  const globals = loadStarterScript('light-planner', 'ephemeris.js', 'light-planner-model.js');
  return { M: globals.PlannerModel as Model, E: globals.Ephemeris as Ephemeris };
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const HARBOUR: Place = { id: 'p1', name: 'Harbour', lat: 40.7128, lon: -74.006 };
const RIDGE: Place = { id: 'p2', name: 'Ridge', lat: -33.8688, lon: 151.2093 };
// 2026-06-21 at UTC-4, the Harbour's local day.
const JUNE_21 = Date.UTC(2026, 5, 21, 4);
const day = (from: number, key = 'd'): Day => ({ from, to: from + 24 * HOUR, key });

describe('the light windows of a day', () => {
  it('are blue then golden in the morning and golden then blue in the evening', () => {
    const { M } = load();
    const windows = M.lightWindows(day(JUNE_21), HARBOUR);
    expect(windows.map((w) => `${w.part} ${w.kind}`)).toEqual([
      'morning blue',
      'morning golden',
      'evening golden',
      'evening blue',
    ]);
    const [mBlue, mGolden, eGolden, eBlue] = windows;
    expect(mBlue.to).toBe(mGolden.from);
    expect(eGolden.to).toBe(eBlue.from);
  });

  it('start and end where the sun crosses the band edges', () => {
    const { M, E } = load();
    const at = (altitude: number, rising: boolean) =>
      E.sunCrossings(JUNE_21, JUNE_21 + 24 * HOUR, HARBOUR.lat, HARBOUR.lon, altitude).find(
        (c) => c.rising === rising,
      )!.at;
    const evening = M.lightWindows(day(JUNE_21), HARBOUR).find(
      (w) => w.part === 'evening' && w.kind === 'golden',
    )!;
    expect(evening.from).toBe(at(M.GOLDEN[1], false));
    expect(evening.to).toBe(at(M.GOLDEN[0], false));
    // An evening golden hour in New York in June is about an hour and a half long.
    expect((evening.to - evening.from) / MINUTE).toBeGreaterThan(60);
    expect((evening.to - evening.from) / MINUTE).toBeLessThan(120);
  });

  it('is the whole day, clipped to it, when the sun never leaves the band', () => {
    const { M } = load();
    const bands = M.sunBands(JUNE_21, JUNE_21 + 24 * HOUR, 78.2232, 15.6267, -90, 90);
    expect(bands).toEqual([{ from: JUNE_21, to: JUNE_21 + 24 * HOUR }]);
  });
});

describe('busy time', () => {
  it('merges timed events and leaves out all-day and unreadable ones', () => {
    const { M } = load();
    const busy = M.busyIntervals({
      events: [
        { start: '2026-06-21T18:00:00-04:00', end: '2026-06-21T19:00:00-04:00', all_day: false },
        { start: '2026-06-21T18:30:00-04:00', end: '2026-06-21T19:30:00-04:00', all_day: false },
        { start: '2026-06-21', end: '2026-06-22', all_day: true },
        { start: 'soon', end: 'later', all_day: false },
        { start: '2026-06-21T21:00:00-04:00', end: '2026-06-21T20:00:00-04:00', all_day: false },
      ],
    });
    expect(busy).toEqual([
      {
        from: Date.parse('2026-06-21T18:00:00-04:00'),
        to: Date.parse('2026-06-21T19:30:00-04:00'),
      },
    ]);
    expect(M.busyIntervals(null)).toEqual([]);
  });

  it('leaves the free parts of a window, dropping slivers under the minimum', () => {
    const { M } = load();
    const t = (h: number, m: number) => JUNE_21 + h * HOUR + m * MINUTE;
    const window = { from: t(18, 0), to: t(19, 0) };
    const busy = [
      { from: t(17, 0), to: t(18, 5) },
      { from: t(18, 20), to: t(18, 30) },
      { from: t(18, 50), to: t(20, 0) },
    ];
    expect(M.MIN_FREE_MS).toBe(15 * MINUTE);
    // 18:05-18:20 is exactly the minimum and stays; 18:30-18:50 stays.
    expect(M.freeParts(window, busy)).toEqual([
      { from: t(18, 5), to: t(18, 20) },
      { from: t(18, 30), to: t(18, 50) },
    ]);
    expect(M.freeParts(window, busy, 20 * MINUTE)).toEqual([{ from: t(18, 30), to: t(18, 50) }]);
    expect(M.freeParts(window, [])).toEqual([window]);
  });
});

describe('free golden hours', () => {
  it('lists every place and day, leaves out a covered window and one already over', () => {
    const { M } = load();
    const days = [day(JUNE_21, '2026-06-21'), day(JUNE_21 + 24 * HOUR, '2026-06-22')];
    const harbourEvening = M.lightWindows(days[0], HARBOUR).find(
      (w) => w.part === 'evening' && w.kind === 'golden',
    )!;
    const busy = [{ from: harbourEvening.from - HOUR, to: harbourEvening.to + HOUR }];
    // Noon on the first day: the Harbour's morning golden hour is over.
    const now = JUNE_21 + 8 * HOUR + 12 * HOUR;
    const found = M.freeGoldenHours(days, [HARBOUR, RIDGE], busy, now);

    expect(found.every((o) => o.kind === 'golden')).toBe(true);
    expect(found.every((o) => o.to > now)).toBe(true);
    for (let i = 1; i < found.length; i++)
      expect(found[i].from).toBeGreaterThanOrEqual(found[i - 1].from);
    const harbour = found.filter((o) => o.place.id === 'p1');
    expect(harbour.map((o) => o.part)).toEqual(['morning', 'evening']);
    expect(harbour.every((o) => o.from > harbourEvening.to)).toBe(true);
    expect(found.filter((o) => o.place.id === 'p2').length).toBeGreaterThan(0);
    for (const o of found) expect(o.free.length).toBeGreaterThan(0);
  });
});

describe('places', () => {
  it('keeps places with a name and real coordinates', () => {
    const { M } = load();
    const places = M.parsePlaces({
      places: [
        { id: 3, name: 'Harbour', lat: 40.7, lon: -74 },
        { id: 4, name: '', lat: 10, lon: 10 },
        { id: 5, name: 'Nowhere', lat: 95, lon: 0 },
        { id: 6, name: 'Bad', lat: '1', lon: 0 },
        null,
      ],
    });
    expect(places).toEqual([
      { id: '3', name: 'Harbour', lat: 40.7, lon: -74 },
      { id: '4', name: 'Place 4', lat: 10, lon: 10 },
    ]);
    expect(M.parsePlaces(undefined)).toEqual([]);
  });
});

describe('days and details', () => {
  it('gives consecutive local days starting at today', () => {
    const { M } = load();
    const now = new Date(2026, 9, 25, 15, 30).getTime();
    const days = M.localDays(now, 14);
    expect(days).toHaveLength(14);
    expect(days[0].from).toBe(new Date(2026, 9, 25).getTime());
    expect(days[0].key).toBe('2026-10-25');
    expect(days[13].key).toBe('2026-11-07');
    for (let i = 1; i < days.length; i++) expect(days[i].from).toBe(days[i - 1].to);
  });

  it("summarises a day's sun and moon, and samples the altitudes for the chart", () => {
    const { M } = load();
    const details = M.dayDetails(day(JUNE_21), HARBOUR);
    expect(details.sunrise).toBeLessThan(details.noon!);
    expect(details.noon).toBeLessThan(details.sunset!);
    expect(details.moonrise).not.toBeNull();
    expect(details.windows).toHaveLength(4);
    // USNO for that day (the ephemeris test's query): 48% lit, waxing crescent.
    expect(details.illumination).toBeGreaterThan(0.3);
    expect(details.illumination).toBeLessThan(0.7);
    expect(details.waxing).toBe(true);
    const series = M.altitudeSeries(day(JUNE_21), HARBOUR, 60);
    expect(series.sun).toHaveLength(25);
    expect(series.sun[0].x).toBe(0);
    expect(series.sun[24].x).toBe(24);
    expect(Math.max(...series.sun.map((p) => p.y))).toBeGreaterThan(70);
    expect(series.moon).toHaveLength(25);
  });
});
