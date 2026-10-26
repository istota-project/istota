// The Light planner's logic: a day's sunrise, sunset, golden and blue hours
// and moon for a place, and which golden hours in the coming days fall in
// free time. No DOM and no bridge; light-planner.js draws the result. Reads
// the `Ephemeris` global, which ephemeris.js defines first.
//
// Golden hour is the sun between -4 and +6 degrees, blue hour between -6 and
// -4, both by its geometric altitude. Busy time is every timed calendar
// event; an all-day event (a birthday, a holiday, a trip) does not count, and
// a free stretch shorter than fifteen minutes is not worth listing.

/**
 * @typedef {{from: number, to: number}} PlannerSpan
 * @typedef {{from: number, to: number, key: string}} PlannerDay
 * @typedef {{id: string, name: string, lat: number, lon: number}} PlannerPlace
 * @typedef {{from: number, to: number, kind: 'golden' | 'blue',
 *   part: 'morning' | 'evening'}} PlannerWindow
 * @typedef {PlannerWindow & {place: PlannerPlace, free: PlannerSpan[]}} PlannerOpportunity
 */
(() => {
  const E = /** @type {any} */ (globalThis).Ephemeris;
  const MINUTE = 60_000;
  /** @type {[number, number]} */
  const GOLDEN = [-4, 6];
  /** @type {[number, number]} */
  const BLUE = [-6, -4];
  const MIN_FREE_MS = 15 * MINUTE;

  /** @param {unknown} value */
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);

  /**
   * The places `location.places` answered, those with real coordinates.
   * @param {unknown} result
   * @returns {PlannerPlace[]}
   */
  function parsePlaces(result) {
    const list = /** @type {{places?: unknown}} */ (result || {}).places;
    if (!Array.isArray(list)) return [];
    /** @type {PlannerPlace[]} */
    const places = [];
    for (const item of list) {
      if (!item || typeof item !== 'object') continue;
      const { id, name, lat, lon } = /** @type {Record<string, unknown>} */ (item);
      if (!finite(lat) || !finite(lon)) continue;
      const latitude = /** @type {number} */ (lat);
      const longitude = /** @type {number} */ (lon);
      if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) continue;
      const text = typeof name === 'string' ? name.trim() : '';
      places.push({
        id: String(id),
        name: text || `Place ${id}`,
        lat: latitude,
        lon: longitude,
      });
    }
    return places;
  }

  /** @param {Date} date */
  function dayKey(date) {
    const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  /**
   * `count` local days from the one holding `now`, midnight to midnight in
   * the device's own time zone, so a day around a clock change is 23 or 25
   * hours.
   * @param {number} now
   * @param {number} count
   * @returns {PlannerDay[]}
   */
  function localDays(now, count) {
    const today = new Date(now);
    /** @type {PlannerDay[]} */
    const days = [];
    for (let i = 0; i < count; i++) {
      const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
      const end = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i + 1);
      days.push({ from: start.getTime(), to: end.getTime(), key: dayKey(start) });
    }
    return days;
  }

  /**
   * The stretches of [from, to) with the sun between `lo` and `hi` degrees.
   * @param {number} from
   * @param {number} to
   * @param {number} lat
   * @param {number} lon
   * @param {number} lo
   * @param {number} hi
   * @returns {PlannerSpan[]}
   */
  function sunBands(from, to, lat, lon, lo, hi) {
    const start = E.sun(from, lat, lon).altitude;
    let aboveLo = start >= lo;
    let aboveHi = start >= hi;
    const edges = [
      ...E.sunCrossings(from, to, lat, lon, lo).map((/** @type {any} */ c) => ({
        ...c,
        edge: 'lo',
      })),
      ...E.sunCrossings(from, to, lat, lon, hi).map((/** @type {any} */ c) => ({
        ...c,
        edge: 'hi',
      })),
    ].sort((a, b) => a.at - b.at);
    /** @type {PlannerSpan[]} */
    const bands = [];
    let openedAt = aboveLo && !aboveHi ? from : null;
    for (const edge of edges) {
      if (edge.edge === 'lo') aboveLo = edge.rising;
      else aboveHi = edge.rising;
      const inside = aboveLo && !aboveHi;
      if (inside && openedAt === null) openedAt = edge.at;
      if (!inside && openedAt !== null) {
        if (edge.at > openedAt) bands.push({ from: openedAt, to: edge.at });
        openedAt = null;
      }
    }
    if (openedAt !== null && to > openedAt) bands.push({ from: openedAt, to });
    return bands;
  }

  /**
   * Morning when the sun is climbing through the band's middle.
   * @param {PlannerSpan} span
   * @param {PlannerPlace} place
   * @returns {'morning' | 'evening'}
   */
  function partOf(span, place) {
    const mid = (span.from + span.to) / 2;
    const before = E.sun(mid - MINUTE, place.lat, place.lon).altitude;
    const after = E.sun(mid + MINUTE, place.lat, place.lon).altitude;
    return after >= before ? 'morning' : 'evening';
  }

  /**
   * A day's golden and blue hours at a place, in time order.
   * @param {PlannerSpan} day
   * @param {PlannerPlace} place
   * @returns {PlannerWindow[]}
   */
  function lightWindows(day, place) {
    /** @type {PlannerWindow[]} */
    const windows = [];
    for (const [kind, [lo, hi]] of /** @type {const} */ ([
      ['golden', GOLDEN],
      ['blue', BLUE],
    ])) {
      for (const band of sunBands(day.from, day.to, place.lat, place.lon, lo, hi)) {
        windows.push({ ...band, kind, part: partOf(band, place) });
      }
    }
    return windows.sort((a, b) => a.from - b.from);
  }

  /**
   * Busy time from `calendar.list`'s answer: its timed events, merged.
   * @param {unknown} result
   * @returns {PlannerSpan[]}
   */
  function busyIntervals(result) {
    const events = /** @type {{events?: unknown}} */ (result || {}).events;
    if (!Array.isArray(events)) return [];
    /** @type {PlannerSpan[]} */
    const spans = [];
    for (const event of events) {
      if (!event || typeof event !== 'object' || event.all_day) continue;
      const from = typeof event.start === 'string' ? Date.parse(event.start) : NaN;
      const to = typeof event.end === 'string' ? Date.parse(event.end) : NaN;
      if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) continue;
      spans.push({ from, to });
    }
    spans.sort((a, b) => a.from - b.from);
    /** @type {PlannerSpan[]} */
    const merged = [];
    for (const span of spans) {
      const last = merged[merged.length - 1];
      if (last && span.from <= last.to) last.to = Math.max(last.to, span.to);
      else merged.push({ ...span });
    }
    return merged;
  }

  /**
   * What of `window` no busy span covers, in pieces at least `minimum` long.
   * @param {PlannerSpan} window
   * @param {PlannerSpan[]} busy merged and in time order
   * @param {number} [minimum]
   * @returns {PlannerSpan[]}
   */
  function freeParts(window, busy, minimum = MIN_FREE_MS) {
    /** @type {PlannerSpan[]} */
    const free = [];
    let cursor = window.from;
    for (const span of busy) {
      if (span.to <= cursor) continue;
      if (span.from >= window.to) break;
      if (span.from > cursor) free.push({ from: cursor, to: Math.min(span.from, window.to) });
      cursor = Math.max(cursor, span.to);
      if (cursor >= window.to) break;
    }
    if (cursor < window.to) free.push({ from: cursor, to: window.to });
    return free.filter((part) => part.to - part.from >= minimum);
  }

  /**
   * Every golden hour over `days` at each place with free time in it, not
   * yet over at `now`, in time order. `free` is only the part still to come.
   * @param {PlannerDay[]} days
   * @param {PlannerPlace[]} places
   * @param {PlannerSpan[]} busy
   * @param {number} now
   * @returns {PlannerOpportunity[]}
   */
  function freeGoldenHours(days, places, busy, now) {
    /** @type {PlannerOpportunity[]} */
    const found = [];
    for (const day of days) {
      for (const place of places) {
        for (const window of lightWindows(day, place)) {
          if (window.kind !== 'golden' || window.to <= now) continue;
          const free = freeParts({ from: Math.max(window.from, now), to: window.to }, busy);
          if (free.length) found.push({ ...window, place, free });
        }
      }
    }
    return found.sort((a, b) => a.from - b.from || a.place.name.localeCompare(b.place.name));
  }

  /**
   * A day at a place: the first sunrise, sunset and solar noon, the first
   * moonrise and moonset (each null when the day has none), the moon's lit
   * fraction at noon, and the light windows.
   * @param {PlannerSpan} day
   * @param {PlannerPlace} place
   */
  function dayDetails(day, place) {
    const sun = E.sunDay(day.from, day.to, place.lat, place.lon);
    const moon = E.moonDay(day.from, day.to, place.lat, place.lon);
    const middle = E.moon((day.from + day.to) / 2, place.lat, place.lon);
    return {
      sunrise: sun.rise,
      sunset: sun.set,
      noon: sun.noon,
      moonrise: moon.rise,
      moonset: moon.set,
      illumination: middle.illumination,
      waxing: middle.waxing,
      windows: lightWindows(day, place),
    };
  }

  /**
   * Sun and moon altitude through a day, for the chart: x is hours since
   * the day began, y degrees.
   * @param {PlannerSpan} day
   * @param {PlannerPlace} place
   * @param {number} stepMinutes
   */
  function altitudeSeries(day, place, stepMinutes) {
    /** @type {Array<{x: number, y: number}>} */
    const sun = [];
    /** @type {Array<{x: number, y: number}>} */
    const moon = [];
    for (let at = day.from; at <= day.to; at += stepMinutes * MINUTE) {
      const x = Math.round(((at - day.from) / (60 * MINUTE)) * 100) / 100;
      sun.push({ x, y: Math.round(E.sun(at, place.lat, place.lon).altitude * 10) / 10 });
      moon.push({ x, y: Math.round(E.moon(at, place.lat, place.lon).altitude * 10) / 10 });
    }
    return { sun, moon };
  }

  /** @type {any} */ (globalThis).PlannerModel = Object.freeze({
    GOLDEN,
    BLUE,
    MIN_FREE_MS,
    parsePlaces,
    localDays,
    sunBands,
    lightWindows,
    busyIntervals,
    freeParts,
    freeGoldenHours,
    dayDetails,
    altitudeSeries,
  });
})();
