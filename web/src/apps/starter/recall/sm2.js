// SM-2 scheduling for Recall: what one answer does to a card's review state.
// No DOM and no bridge; recall.js stores the result, and sm2.test.ts loads
// this file on its own.
//
// The review state, kept in `app.recall` under `review:<id>`:
//   {due, interval, ease, reps, lapses, history}
// `due` is a day (`YYYY-MM-DD`), `interval` whole days, `ease` the SM-2 easiness
// factor, `reps` the run of passing answers, `lapses` the misses so far and
// `history` the newest answers as {on, grade, interval}.
//
// The algorithm is SuperMemo's SM-2 as usually implemented: a grade of 3 or
// more passes (intervals 1, 6, then the last interval times the ease held
// before this answer), a grade under 3 starts the run again at 1 day, and the
// ease moves by 0.1 - (5 - q)(0.08 + (5 - q)0.02) on every answer, never
// below 1.3. Ease is kept to two decimals so stored values stay readable.

/**
 * @typedef {{on: string, grade: number, interval: number}} RecallAnswer
 * @typedef {{due: string | null, interval: number, ease: number, reps: number,
 *   lapses: number, history: RecallAnswer[], [extra: string]: unknown}} RecallReview
 */
(() => {
  const INITIAL_EASE = 2.5;
  const MIN_EASE = 1.3;
  const HISTORY_MAX = 100;
  // Without a ceiling a long run of passes grows past what a Date can hold.
  const MAX_INTERVAL = 36500;
  const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

  /** @returns {RecallReview} */
  function newReview() {
    return { due: null, interval: 0, ease: INITIAL_EASE, reps: 0, lapses: 0, history: [] };
  }

  /**
   * @param {unknown} value
   * @returns {string | null}
   */
  function parseDay(value) {
    if (typeof value !== 'string') return null;
    const match = DAY.exec(value);
    if (!match) return null;
    const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
    const date = new Date(Date.UTC(year, month - 1, day));
    if (date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
    return value;
  }

  /**
   * A whole number at least `min`, from a number or a numeric string.
   * @param {unknown} value
   * @param {number} min
   * @param {number} fallback
   */
  function count(value, min, fallback) {
    const number = typeof value === 'string' && value.trim() ? Number(value) : value;
    if (typeof number !== 'number' || !Number.isFinite(number)) return fallback;
    return Math.max(min, Math.round(number));
  }

  /** @param {number} ease */
  function roundEase(ease) {
    return Math.max(MIN_EASE, Math.round(ease * 100) / 100);
  }

  /**
   * A stored review, normalized; anything unreadable falls back to a new card's
   * value, and fields Recall does not know are kept.
   * @param {unknown} raw
   * @returns {RecallReview}
   */
  function parseReview(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return newReview();
    const value = /** @type {Record<string, unknown>} */ (raw);
    const ease = typeof value.ease === 'number' && Number.isFinite(value.ease) ? value.ease : null;
    /** @type {RecallAnswer[]} */
    const history = [];
    if (Array.isArray(value.history)) {
      for (const entry of value.history) {
        if (!entry || typeof entry !== 'object') continue;
        const on = parseDay(entry.on);
        const grade = entry.grade;
        if (on && typeof grade === 'number' && isGrade(grade)) {
          history.push({ on, grade, interval: count(entry.interval, 0, 0) });
        }
      }
    }
    return {
      ...value,
      due: parseDay(value.due),
      interval: Math.min(MAX_INTERVAL, count(value.interval, 0, 0)),
      ease: ease === null ? INITIAL_EASE : roundEase(ease),
      reps: count(value.reps, 0, 0),
      lapses: count(value.lapses, 0, 0),
      history: history.slice(-HISTORY_MAX),
    };
  }

  /** @param {number} quality */
  function isGrade(quality) {
    return Number.isInteger(quality) && quality >= 0 && quality <= 5;
  }

  /** @param {number} quality */
  function isMiss(quality) {
    return quality < 3;
  }

  /**
   * The interval this answer would give, in days.
   * @param {RecallReview} review
   * @param {number} quality
   */
  function nextInterval(review, quality) {
    if (isMiss(quality)) return 1;
    if (review.reps === 0) return 1;
    if (review.reps === 1) return 6;
    return Math.min(MAX_INTERVAL, Math.max(1, Math.round(review.interval * review.ease)));
  }

  /**
   * @param {string} day `YYYY-MM-DD`
   * @param {number} days
   */
  function addDays(day, days) {
    const [year, month, date] = day.split('-').map(Number);
    return new Date(Date.UTC(year, month - 1, date + days)).toISOString().slice(0, 10);
  }

  /**
   * The review after answering with `quality` (0 to 5) on `today`.
   * @param {RecallReview} review
   * @param {number} quality
   * @param {string} today `YYYY-MM-DD`, the user's own day
   * @returns {RecallReview}
   */
  function grade(review, quality, today) {
    if (!isGrade(quality)) throw new RangeError('A grade is a whole number from 0 to 5.');
    const interval = nextInterval(review, quality);
    const miss = isMiss(quality);
    const step = 5 - quality;
    return {
      ...review,
      due: addDays(today, interval),
      interval,
      ease: roundEase(review.ease + (0.1 - step * (0.08 + step * 0.02))),
      reps: miss ? 0 : review.reps + 1,
      lapses: miss ? review.lapses + 1 : review.lapses,
      history: [...review.history, { on: today, grade: quality, interval }].slice(-HISTORY_MAX),
    };
  }

  /** @type {any} */ (globalThis).SM2 = Object.freeze({
    INITIAL_EASE,
    MIN_EASE,
    HISTORY_MAX,
    MAX_INTERVAL,
    newReview,
    parseReview,
    parseDay,
    isMiss,
    nextInterval,
    addDays,
    grade,
  });
})();
