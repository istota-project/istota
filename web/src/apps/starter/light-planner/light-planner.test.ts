// light-planner.js against a fake `istota.skill` and `istota.calendar`, with
// the real kit components, ephemeris.js and light-planner-model.js, run the
// way the installed document runs them.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ROOT = join(process.cwd(), 'src', 'apps');
const read = (path: string) => readFileSync(join(ROOT, path), 'utf8');
const INDEX = read('starter/light-planner/index.html');
const BODY = INDEX.slice(INDEX.indexOf('<body>') + 6, INDEX.indexOf('<script'));

type Globals = Record<string, unknown>;

class IstotaError extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

const PLACES = {
  places: [
    { id: 1, name: 'Harbour', lat: 40.7128, lon: -74.006, radius_meters: 100 },
    { id: 2, name: 'Ridge', lat: 40.9, lon: -73.9, radius_meters: 100 },
  ],
};

type Answer = unknown | IstotaError;

function fakeIstota(places: Answer, calendar: Answer) {
  const answer = async (value: Answer) => {
    if (value instanceof IstotaError) throw value;
    return value;
  };
  return {
    env: { visibility: 'visible' },
    on: () => () => {},
    skill: vi.fn(async () => answer(places)),
    calendar: { list: vi.fn(async () => answer(calendar)) },
  };
}

function run(places: Answer = PLACES, calendar: Answer = { calendars: [], events: [] }) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
  document.body.innerHTML = BODY;
  const istota = fakeIstota(places, calendar);
  (globalThis as Globals).istota = istota;
  for (const file of [
    'kit/components.js',
    'starter/light-planner/ephemeris.js',
    'starter/light-planner/light-planner-model.js',
    'starter/light-planner/light-planner.js',
  ]) {
    new Function(read(file))();
  }
  return istota;
}

const $ = (id: string) => document.getElementById(id) as HTMLElement;
const rows = () =>
  [...document.querySelectorAll('#times tr')].map((tr) =>
    [...tr.children].map((td) => td.textContent),
  );
const freeRows = () => [...document.querySelectorAll('#free .planner-row')];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 5, 21, 0, 30));
  document.body.innerHTML = '';
});

afterEach(() => {
  vi.useRealTimers();
  for (const name of ['istota', 'Ephemeris', 'PlannerModel']) delete (globalThis as Globals)[name];
});

describe('light-planner.js', () => {
  it("draws today's sun and moon for the first place, and reads two weeks of calendar", async () => {
    const istota = run();
    await vi.waitFor(() => expect($('today-section').hidden).toBe(false));
    expect(istota.skill).toHaveBeenCalledWith('location.places');
    expect(istota.calendar.list).toHaveBeenCalledWith({ from: '2026-06-21', to: '2026-07-05' });
    expect($('today-heading').textContent).toBe('Today at Harbour');
    expect(rows().map(([what]) => what)).toEqual([
      'Morning blue hour',
      'Morning golden hour',
      'Sunrise',
      'Solar noon',
      'Sunset',
      'Evening golden hour',
      'Evening blue hour',
      'Moonrise',
      'Moonset',
      'Moon',
    ]);
    expect(rows().find(([what]) => what === 'Moon')?.[1]).toMatch(/^\d+% lit, waxing$/);
    expect($('now').textContent).toMatch(/^Now the sun is \d+° (up in the \w+|below the horizon)/);
    const chart = $('chart') as HTMLElement & { data: { series: Array<{ name: string }> } };
    expect(chart.data.series.map((s) => s.name)).toEqual(['Sun', 'Moon']);
    expect(chart.querySelector('path.istota-chart-line')).not.toBeNull();
    expect($('empty').hidden).toBe(true);
  });

  it('lists the free golden hours to come, and switches place from the picker', async () => {
    run();
    await vi.waitFor(() => expect(freeRows().length).toBeGreaterThan(0));
    // Two places, two golden hours a day, fourteen days, less any already
    // over or clipped by a day's edge in the zone the tests run in.
    expect(freeRows().length).toBeGreaterThanOrEqual(52);
    expect(freeRows().length).toBeLessThanOrEqual(56);
    expect(freeRows()[0].querySelector('.planner-row-meta')?.textContent).toMatch(
      /^(Harbour|Ridge) · golden hour /,
    );
    expect($('place-picker').hidden).toBe(false);
    const select = $('place') as HTMLSelectElement;
    select.value = '2';
    select.dispatchEvent(new Event('change'));
    expect($('today-heading').textContent).toBe('Today at Ridge');
  });

  it('leaves out golden hours the calendar fills', async () => {
    run(PLACES, {
      events: [
        {
          start: new Date(2026, 5, 20).toISOString(),
          end: new Date(2026, 6, 10).toISOString(),
          all_day: false,
        },
      ],
    });
    await vi.waitFor(() => expect($('free-empty').hidden).toBe(false));
    expect($('free-empty').textContent).toBe('No free golden hour in the next 14 days.');
    expect(freeRows()).toHaveLength(0);
  });

  it('says so when the calendar was not allowed, and lists every golden hour', async () => {
    run(PLACES, new IstotaError('not_granted', 'not granted'));
    await vi.waitFor(() => expect(freeRows().length).toBeGreaterThanOrEqual(52));
    expect($('calendar-note').textContent).toBe(
      'Light planner has not been allowed to read your calendar, so every golden hour is listed, busy or not.',
    );
  });

  it('asks for the places when they were not allowed', async () => {
    run(new IstotaError('not_granted', 'not granted'));
    await vi.waitFor(() => expect($('empty').hidden).toBe(false));
    expect($('empty-title').textContent).toBe('Light planner cannot see your places.');
    expect($('today-section').hidden).toBe(true);
    expect($('free-section').hidden).toBe(true);
  });

  it('says there are no places when Location has none', async () => {
    run({ places: [] });
    await vi.waitFor(() => expect($('empty').hidden).toBe(false));
    expect($('empty-title').textContent).toBe('No places yet.');
    expect($('status').textContent).toBe('');
  });
});
