// The Light planner's ephemeris: where the Sun and the Moon are, and when they
// cross a given altitude, for a place on Earth. Plain JS with no DOM and no
// bridge; light-planner-model.js reads it, and ephemeris.test.ts checks it
// against NOAA's, the USNO's and Meeus's published values.
//
// Kept small on purpose: a later build may replace this file with a wasm one,
// so everything goes through the seven functions at the bottom, which take a
// UTC instant in milliseconds and degrees (north and east positive), and
// return degrees and milliseconds.
//
// The Sun is NOAA's solar position (Meeus, Astronomical Algorithms, ch. 25,
// low accuracy), good to about 0.01 degrees. The Moon is Meeus ch. 47 with the
// leading terms of tables 47.A and 47.B, good to a few hundredths of a degree.
// Rise and set follow Meeus ch. 15: the Sun's upper limb with refraction at
// -0.8333 degrees, and the Moon at 0.7275 times its parallax less 0.5667.
// Times are found by sampling every ten minutes and bisecting each change.

(() => {
  const RAD = Math.PI / 180;
  const DAY_MS = 86_400_000;
  // TT - UT in the 2020s. A minute's error here moves the Moon by half an
  // arcminute, which no time on the page can show.
  const DELTA_T_SECONDS = 69;
  const SUN_HORIZON = -0.8333;
  const STEP_MS = 10 * 60_000;
  const KM_PER_AU = 149_597_870.7;
  const EARTH_RADIUS_KM = 6378.14;

  /** @param {number} degrees */
  const sin = (degrees) => Math.sin(degrees * RAD);
  /** @param {number} degrees */
  const cos = (degrees) => Math.cos(degrees * RAD);
  /** @param {number} degrees */
  const norm = (degrees) => ((degrees % 360) + 360) % 360;

  /** @param {number} ms */
  function julianDay(ms) {
    return ms / DAY_MS + 2440587.5;
  }

  /**
   * Julian centuries from J2000.0 in dynamical time.
   * @param {number} ms
   */
  function centuries(ms) {
    return (julianDay(ms) + DELTA_T_SECONDS / 86_400 - 2451545) / 36525;
  }

  /**
   * Greenwich mean sidereal time, Meeus 12.4.
   * @param {number} ms
   */
  function siderealTime(ms) {
    const jd = julianDay(ms);
    const t = (jd - 2451545) / 36525;
    return norm(
      280.46061837 +
        360.98564736629 * (jd - 2451545) +
        0.000387933 * t * t -
        (t * t * t) / 38710000,
    );
  }

  /**
   * Mean obliquity, Meeus 22.2, and the nutation in longitude and obliquity
   * from its four largest terms.
   * @param {number} t
   */
  function nutation(t) {
    const omega = 125.04452 - 1934.136261 * t;
    const sunL = 280.4665 + 36000.7698 * t;
    const moonL = 218.3165 + 481267.8813 * t;
    const dPsi =
      (-17.2 * sin(omega) - 1.32 * sin(2 * sunL) - 0.23 * sin(2 * moonL) + 0.21 * sin(2 * omega)) /
      3600;
    const dEps =
      (9.2 * cos(omega) + 0.57 * cos(2 * sunL) + 0.1 * cos(2 * moonL) - 0.09 * cos(2 * omega)) /
      3600;
    const eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60;
    return { dPsi, eps: eps0 + dEps, eps0 };
  }

  /**
   * Ecliptic to equatorial coordinates.
   * @param {number} lambda
   * @param {number} beta
   * @param {number} eps
   */
  function equatorial(lambda, beta, eps) {
    const ra = Math.atan2(sin(lambda) * cos(eps) - Math.tan(beta * RAD) * sin(eps), cos(lambda));
    const dec = Math.asin(sin(beta) * cos(eps) + cos(beta) * sin(eps) * sin(lambda));
    return { ra: norm(ra / RAD), dec: dec / RAD };
  }

  /**
   * The Sun's apparent place, NOAA's algorithm (Meeus 25, low accuracy).
   * @param {number} ms
   */
  function sunPlace(ms) {
    const t = centuries(ms);
    const l0 = norm(280.46646 + t * (36000.76983 + t * 0.0003032));
    const m = 357.52911 + t * (35999.05029 - 0.0001537 * t);
    const e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t);
    const c =
      sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t)) +
      sin(2 * m) * (0.019993 - 0.000101 * t) +
      sin(3 * m) * 0.000289;
    const trueLongitude = l0 + c;
    const anomaly = m + c;
    const distance = (1.000001018 * (1 - e * e)) / (1 + e * cos(anomaly));
    const omega = 125.04 - 1934.136 * t;
    const longitude = norm(trueLongitude - 0.00569 - 0.00478 * sin(omega));
    const eps = nutation(t).eps0 + 0.00256 * cos(omega);
    return { longitude, distance, ...equatorial(longitude, 0, eps) };
  }

  // Meeus table 47.A: multiples of D, M, M', F, then the sine coefficient of
  // the longitude (1e-6 degrees) and the cosine coefficient of the distance
  // (1e-3 km). The leading terms only; the rest are each under 0.002 degrees.
  const LONGITUDE_DISTANCE = [
    [0, 0, 1, 0, 6288774, -20905355],
    [2, 0, -1, 0, 1274027, -3699111],
    [2, 0, 0, 0, 658314, -2955968],
    [0, 0, 2, 0, 213618, -569925],
    [0, 1, 0, 0, -185116, 48888],
    [0, 0, 0, 2, -114332, -3149],
    [2, 0, -2, 0, 58793, 246158],
    [2, -1, -1, 0, 57066, -152138],
    [2, 0, 1, 0, 53322, -170733],
    [2, -1, 0, 0, 45758, -204586],
    [0, 1, -1, 0, -40923, -129620],
    [1, 0, 0, 0, -34720, 108743],
    [0, 1, 1, 0, -30383, 104755],
    [2, 0, 0, -2, 15327, 10321],
    [0, 0, 1, 2, -12528, 0],
    [0, 0, 1, -2, 10980, 79661],
    [4, 0, -1, 0, 10675, -34782],
    [0, 0, 3, 0, 10034, -23210],
    [4, 0, -2, 0, 8548, -21636],
    [2, 1, -1, 0, -7888, 24208],
    [2, 1, 0, 0, -6766, 30824],
    [1, 0, -1, 0, -5163, -8379],
    [1, 1, 0, 0, 4987, -16675],
    [2, -1, 1, 0, 4036, -12831],
    [2, 0, 2, 0, 3994, -10445],
    [4, 0, 0, 0, 3861, -11650],
    [2, 0, -3, 0, 3665, 14403],
    [0, 1, -2, 0, -2689, -7003],
    [2, 0, -1, 2, -2602, 0],
    [2, -1, -2, 0, 2390, 10056],
    [1, 0, 1, 0, -2348, 6322],
    [2, -2, 0, 0, 2236, -9884],
    [0, 1, 2, 0, -2120, 5751],
    [0, 2, 0, 0, -2069, 0],
  ];

  // Meeus table 47.B: multiples of D, M, M', F and the sine coefficient of
  // the latitude (1e-6 degrees), leading terms.
  const LATITUDE = [
    [0, 0, 0, 1, 5128122],
    [0, 0, 1, 1, 280602],
    [0, 0, 1, -1, 277693],
    [2, 0, 0, -1, 173237],
    [2, 0, -1, 1, 55413],
    [2, 0, -1, -1, 46271],
    [2, 0, 0, 1, 32573],
    [0, 0, 2, 1, 17198],
    [2, 0, 1, -1, 9266],
    [0, 0, 2, -1, 8822],
    [2, -1, 0, -1, 8216],
    [2, 0, -2, -1, 4324],
    [2, 0, 1, 1, 4200],
    [2, 1, 0, -1, -3359],
    [2, -1, -1, 1, 2463],
    [2, -1, 0, 1, 2211],
    [2, -1, -1, -1, 2065],
    [0, 1, -1, -1, -1870],
    [4, 0, -1, -1, 1828],
    [0, 1, 0, 1, -1794],
  ];

  /**
   * The Moon's geocentric place, Meeus 47.
   * @param {number} ms
   */
  function moonPlace(ms) {
    const t = centuries(ms);
    const t2 = t * t;
    const t3 = t2 * t;
    const t4 = t3 * t;
    const lp = norm(
      218.3164477 + 481267.88123421 * t - 0.0015786 * t2 + t3 / 538841 - t4 / 65194000,
    );
    const d = norm(
      297.8501921 + 445267.1114034 * t - 0.0018819 * t2 + t3 / 545868 - t4 / 113065000,
    );
    const m = norm(357.5291092 + 35999.0502909 * t - 0.0001536 * t2 + t3 / 24490000);
    const mp = norm(134.9633964 + 477198.8675055 * t + 0.0087414 * t2 + t3 / 69699 - t4 / 14712000);
    const f = norm(93.272095 + 483202.0175233 * t - 0.0036539 * t2 - t3 / 3526000 + t4 / 863310000);
    const a1 = 119.75 + 131.849 * t;
    const a2 = 53.09 + 479264.29 * t;
    const a3 = 313.45 + 481266.484 * t;
    const e = 1 - 0.002516 * t - 0.0000074 * t2;

    let sumL = 0;
    let sumR = 0;
    for (const [cd, cm, cmp, cf, cl, cr] of LONGITUDE_DISTANCE) {
      const arg = cd * d + cm * m + cmp * mp + cf * f;
      const factor = Math.abs(cm) === 1 ? e : Math.abs(cm) === 2 ? e * e : 1;
      sumL += cl * factor * sin(arg);
      sumR += cr * factor * cos(arg);
    }
    let sumB = 0;
    for (const [cd, cm, cmp, cf, cb] of LATITUDE) {
      const factor = Math.abs(cm) === 1 ? e : Math.abs(cm) === 2 ? e * e : 1;
      sumB += cb * factor * sin(cd * d + cm * m + cmp * mp + cf * f);
    }
    sumL += 3958 * sin(a1) + 1962 * sin(lp - f) + 318 * sin(a2);
    sumB +=
      -2235 * sin(lp) +
      382 * sin(a3) +
      175 * sin(a1 - f) +
      175 * sin(a1 + f) +
      127 * sin(lp - mp) -
      115 * sin(lp + mp);

    const longitude = norm(lp + sumL / 1e6);
    const latitude = sumB / 1e6;
    const distance = 385000.56 + sumR / 1000;
    const nut = nutation(t);
    return {
      longitude,
      latitude,
      distance,
      parallax: Math.asin(EARTH_RADIUS_KM / distance) / RAD,
      ...equatorial(longitude + nut.dPsi, latitude, nut.eps),
    };
  }

  /**
   * Altitude and azimuth (from north, eastward) of a body at `ra`, `dec`.
   * @param {number} ms
   * @param {number} lat
   * @param {number} lon
   * @param {number} ra
   * @param {number} dec
   */
  function horizontal(ms, lat, lon, ra, dec) {
    const hourAngle = siderealTime(ms) + lon - ra;
    const altitude = Math.asin(sin(lat) * sin(dec) + cos(lat) * cos(dec) * cos(hourAngle)) / RAD;
    const azimuth = norm(
      Math.atan2(sin(hourAngle), cos(hourAngle) * sin(lat) - Math.tan(dec * RAD) * cos(lat)) / RAD +
        180,
    );
    return { altitude, azimuth, hourAngle: norm(hourAngle + 180) - 180 };
  }

  /**
   * Where the Sun is: geometric altitude, azimuth, right ascension and
   * declination, in degrees.
   * @param {number} ms
   * @param {number} lat
   * @param {number} lon
   */
  function sun(ms, lat, lon) {
    const place = sunPlace(ms);
    const { altitude, azimuth } = horizontal(ms, lat, lon, place.ra, place.dec);
    return { altitude, azimuth, ra: place.ra, dec: place.dec };
  }

  /**
   * Where the Moon is. `altitude` is seen from the place (parallax taken
   * off); `longitude` and `latitude` are geocentric ecliptic, `distance` in
   * km; `illumination` is the lit fraction of the disc, 0 to 1.
   * @param {number} ms
   * @param {number} lat
   * @param {number} lon
   */
  function moon(ms, lat, lon) {
    const place = moonPlace(ms);
    const view = horizontal(ms, lat, lon, place.ra, place.dec);
    const solar = sunPlace(ms);
    const sunKm = solar.distance * KM_PER_AU;
    // Meeus 48.2 and 48.3, with the elongation from ecliptic coordinates.
    const elongation = Math.acos(cos(place.latitude) * cos(place.longitude - solar.longitude));
    const phaseAngle = Math.atan2(
      sunKm * Math.sin(elongation),
      place.distance - sunKm * Math.cos(elongation),
    );
    return {
      altitude: view.altitude - place.parallax * cos(view.altitude),
      azimuth: view.azimuth,
      ra: place.ra,
      dec: place.dec,
      longitude: place.longitude,
      latitude: place.latitude,
      distance: place.distance,
      illumination: (1 + Math.cos(phaseAngle)) / 2,
      waxing: norm(place.longitude - solar.longitude) < 180,
    };
  }

  /**
   * Every instant in [from, to) where `fn` changes sign, rising (negative to
   * positive) or setting. `skip` refuses a step across a discontinuity.
   * @param {(ms: number) => number} fn
   * @param {number} from
   * @param {number} to
   * @param {(a: number, b: number) => boolean} [skip]
   * @returns {Array<{at: number, rising: boolean}>}
   */
  function signChanges(fn, from, to, skip) {
    /** @type {Array<{at: number, rising: boolean}>} */
    const found = [];
    let a = from;
    let fa = fn(a);
    while (a < to) {
      const b = Math.min(a + STEP_MS, to);
      const fb = fn(b);
      if (fa < 0 !== fb < 0 && !(skip && skip(fa, fb))) {
        let lo = a;
        let hi = b;
        let flo = fa;
        while (hi - lo > 500) {
          const mid = (lo + hi) / 2;
          const fmid = fn(mid);
          if (fmid < 0 === flo < 0) {
            lo = mid;
            flo = fmid;
          } else {
            hi = mid;
          }
        }
        const at = Math.round((lo + hi) / 2);
        if (at < to) found.push({ at, rising: fa < 0 });
      }
      a = b;
      fa = fb;
    }
    return found;
  }

  /**
   * When the Sun's centre passes `altitude` degrees (geometric), in [from, to).
   * Rise and set are at SUN_HORIZON.
   * @param {number} from
   * @param {number} to
   * @param {number} lat
   * @param {number} lon
   * @param {number} altitude
   */
  function sunCrossings(from, to, lat, lon, altitude) {
    return signChanges((ms) => sun(ms, lat, lon).altitude - altitude, from, to);
  }

  /**
   * Moonrise and moonset in [from, to), Meeus 15's standard altitude.
   * @param {number} from
   * @param {number} to
   * @param {number} lat
   * @param {number} lon
   */
  function moonCrossings(from, to, lat, lon) {
    return signChanges(
      (ms) => {
        const place = moonPlace(ms);
        const geocentric = horizontal(ms, lat, lon, place.ra, place.dec).altitude;
        return geocentric - (0.7275 * place.parallax - 0.5667);
      },
      from,
      to,
    );
  }

  /**
   * Upper transits (the body due south or north, highest) in [from, to).
   * @param {'sun' | 'moon'} body
   * @param {number} from
   * @param {number} to
   * @param {number} lat
   * @param {number} lon
   */
  function transits(body, from, to, lat, lon) {
    const placeOf = body === 'moon' ? moonPlace : sunPlace;
    return signChanges(
      (ms) => {
        const place = placeOf(ms);
        return horizontal(ms, lat, lon, place.ra, place.dec).hourAngle;
      },
      from,
      to,
      // The hour angle wraps from +180 to -180 at lower transit.
      (a, b) => Math.abs(a - b) > 180,
    )
      .filter((c) => c.rising)
      .map((c) => c.at);
  }

  /**
   * The first sunrise, sunset and solar noon in [from, to), each null when
   * there is none (a polar day or night).
   * @param {number} from
   * @param {number} to
   * @param {number} lat
   * @param {number} lon
   */
  function sunDay(from, to, lat, lon) {
    const crossings = sunCrossings(from, to, lat, lon, SUN_HORIZON);
    return {
      rise: crossings.find((c) => c.rising)?.at ?? null,
      set: crossings.find((c) => !c.rising)?.at ?? null,
      noon: transits('sun', from, to, lat, lon)[0] ?? null,
    };
  }

  /**
   * The first moonrise, moonset and transit in [from, to), each null when
   * there is none; most days miss one of the three.
   * @param {number} from
   * @param {number} to
   * @param {number} lat
   * @param {number} lon
   */
  function moonDay(from, to, lat, lon) {
    const crossings = moonCrossings(from, to, lat, lon);
    return {
      rise: crossings.find((c) => c.rising)?.at ?? null,
      set: crossings.find((c) => !c.rising)?.at ?? null,
      transit: transits('moon', from, to, lat, lon)[0] ?? null,
    };
  }

  /** @type {any} */ (globalThis).Ephemeris = Object.freeze({
    DELTA_T_SECONDS,
    SUN_HORIZON,
    sun,
    moon,
    sunCrossings,
    moonCrossings,
    transits,
    sunDay,
    moonDay,
  });
})();
