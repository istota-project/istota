// Ledger's pure logic: reading `app.ledger`, sorting guesses into due, open
// and resolved, the calibration bins and the Brier score. No DOM and no
// bridge; ledger.js does the drawing and the writes, and ledger-model.test.ts
// loads this file on its own.
//
// The data layout in `app.ledger` (documented in docs/features/apps.md):
//   guess:<id>   {id, text, p, resolves_on, outcome, created_at, resolved_at}
// The agent writes these keys too, through the kv skill, and may leave fields
// out or write `p` as a percentage, so every value read is normalized and
// unknown fields are kept.
//
// Every function returns new objects and leaves the list it was given alone.

/**
 * @typedef {{id: string, text: string, p: number | null, resolves_on: string | null,
 *   outcome: boolean | null, created_at: string, resolved_at: string | null,
 *   [extra: string]: unknown}} LedgerGuess
 * @typedef {{guesses: LedgerGuess[], changed: LedgerGuess}} LedgerChange
 * @typedef {{index: number, lo: number, hi: number, count: number, hits: number,
 *   meanP: number | null, observed: number | null}} LedgerBin
 */
(() => {
  const GUESS_PREFIX = 'guess:';
  const ID_MAX = 64;
  const BINS = 10;
  const ID_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyz';
  const DAY = /^(\d{4})-(\d{2})-(\d{2})/;

  /** @param {string} id */
  function guessKey(id) {
    return GUESS_PREFIX + id;
  }

  /** @param {unknown} value */
  function text(value) {
    return typeof value === 'string' ? value : '';
  }

  /**
   * A probability as a fraction in [0, 1]. A number above 1 and up to 100 is
   * read as a percentage, since "log that at 60%" may well arrive as 60.
   * @param {unknown} value
   * @returns {number | null}
   */
  function parseProbability(value) {
    let number = NaN;
    if (typeof value === 'number') number = value;
    else if (typeof value === 'string' && value.trim()) number = Number(value);
    if (!Number.isFinite(number) || number < 0 || number > 100) return null;
    return number > 1 ? number / 100 : number;
  }

  /**
   * A calendar day as `YYYY-MM-DD`; an ISO time is cut to its day.
   * @param {unknown} value
   * @returns {string | null}
   */
  function parseDate(value) {
    if (typeof value !== 'string') return null;
    const match = DAY.exec(value);
    if (!match) return null;
    const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
    const date = new Date(Date.UTC(year, month - 1, day));
    if (date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
    return match[0];
  }

  /**
   * @param {unknown} value
   * @returns {boolean | null}
   */
  function parseOutcome(value) {
    if (value === true || value === 1) return true;
    if (value === false || value === 0) return false;
    return null;
  }

  /** @param {unknown} value */
  function timeOrNull(value) {
    return typeof value === 'string' && value ? value : null;
  }

  /**
   * @param {string} key
   * @param {unknown} raw
   * @returns {LedgerGuess | null}
   */
  function parseGuess(key, raw) {
    if (typeof key !== 'string' || !key.startsWith(GUESS_PREFIX)) return null;
    const id = key.slice(GUESS_PREFIX.length);
    if (!id || id.length > ID_MAX || !raw || typeof raw !== 'object' || Array.isArray(raw)) {
      return null;
    }
    const value = /** @type {Record<string, unknown>} */ (raw);
    return {
      ...value,
      id,
      text: text(value.text),
      p: parseProbability(value.p),
      resolves_on: parseDate(value.resolves_on),
      outcome: parseOutcome(value.outcome),
      created_at: text(value.created_at),
      resolved_at: timeOrNull(value.resolved_at),
    };
  }

  /**
   * @param {Array<{key: string, value: unknown}>} items
   * @returns {LedgerGuess[]}
   */
  function fromStorage(items) {
    /** @type {LedgerGuess[]} */
    const guesses = [];
    for (const item of Array.isArray(items) ? items : []) {
      const guess = item ? parseGuess(item.key, item.value) : null;
      if (guess) guesses.push(guess);
    }
    return guesses;
  }

  /** @param {number} n */
  function pad(n) {
    return String(n).padStart(2, '0');
  }

  /**
   * The user's own calendar day, which is what "resolves on" means to them.
   * @param {Date} date
   */
  function localDay(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  /**
   * @param {LedgerGuess} guess
   * @param {string} today
   */
  function isDue(guess, today) {
    return guess.outcome === null && guess.resolves_on !== null && guess.resolves_on <= today;
  }

  /**
   * @param {string | null} a
   * @param {string | null} b
   */
  function byDay(a, b) {
    if (a === b) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    return a < b ? -1 : 1;
  }

  /**
   * Due (oldest first), open (soonest first, undated last) and resolved
   * (most recently resolved first).
   * @param {LedgerGuess[]} guesses
   * @param {string} today
   */
  function sections(guesses, today) {
    /** @type {LedgerGuess[]} */
    const due = [];
    /** @type {LedgerGuess[]} */
    const open = [];
    /** @type {LedgerGuess[]} */
    const resolved = [];
    for (const guess of guesses) {
      if (guess.outcome !== null) resolved.push(guess);
      else if (isDue(guess, today)) due.push(guess);
      else open.push(guess);
    }
    const soonest = (/** @type {LedgerGuess} */ a, /** @type {LedgerGuess} */ b) =>
      byDay(a.resolves_on, b.resolves_on) || a.created_at.localeCompare(b.created_at);
    due.sort(soonest);
    open.sort(soonest);
    resolved.sort((a, b) => -byDay(a.resolved_at, b.resolved_at) || soonest(a, b));
    return { due, open, resolved };
  }

  /**
   * @param {LedgerGuess[]} guesses
   * @param {{text: string, p: unknown, resolves_on: unknown}} fields
   * @param {string} now
   * @param {string} id
   * @returns {LedgerChange}
   */
  function addGuess(guesses, fields, now, id) {
    /** @type {LedgerGuess} */
    const guess = {
      id,
      text: text(fields.text).trim(),
      p: parseProbability(fields.p),
      resolves_on: parseDate(fields.resolves_on),
      outcome: null,
      created_at: now,
      resolved_at: null,
    };
    return { guesses: [...guesses, guess], changed: guess };
  }

  /**
   * @param {LedgerGuess[]} guesses
   * @param {string} id
   * @param {(guess: LedgerGuess) => LedgerGuess} change
   * @returns {LedgerChange | null}
   */
  function replace(guesses, id, change) {
    const index = guesses.findIndex((g) => g.id === id);
    if (index < 0) return null;
    const changed = change(guesses[index]);
    const next = [...guesses];
    next[index] = changed;
    return { guesses: next, changed };
  }

  /**
   * @param {LedgerGuess[]} guesses
   * @param {string} id
   * @param {Partial<LedgerGuess>} patch
   */
  function updateGuess(guesses, id, patch) {
    return replace(guesses, id, (guess) => {
      const next = { ...guess };
      if ('text' in patch) next.text = text(patch.text).trim();
      if ('p' in patch) next.p = parseProbability(patch.p);
      if ('resolves_on' in patch) next.resolves_on = parseDate(patch.resolves_on);
      return next;
    });
  }

  /**
   * Record what happened, or `null` to reopen the guess.
   * @param {LedgerGuess[]} guesses
   * @param {string} id
   * @param {boolean | null} outcome
   * @param {string} now
   */
  function resolveGuess(guesses, id, outcome, now) {
    return replace(guesses, id, (guess) => ({
      ...guess,
      outcome,
      resolved_at: outcome === null ? null : now,
    }));
  }

  /**
   * @param {LedgerGuess[]} guesses
   * @param {string} id
   */
  function removeGuess(guesses, id) {
    return guesses.filter((g) => g.id !== id);
  }

  /** @param {LedgerGuess[]} guesses */
  function scored(guesses) {
    return guesses.filter((g) => g.outcome !== null && g.p !== null);
  }

  /**
   * The tenth a probability falls in; 100% belongs to the top one. The small
   * nudge keeps 0.7 * 10 = 6.999... in bin 7.
   * @param {number} p
   */
  function binIndex(p) {
    return Math.min(BINS - 1, Math.max(0, Math.floor(p * BINS + 1e-9)));
  }

  /**
   * The mean of (p - outcome)^2 over resolved guesses: 0 is perfect, 0.25 is
   * what saying 50% every time scores.
   * @param {LedgerGuess[]} guesses
   */
  function brierScore(guesses) {
    const resolved = scored(guesses);
    if (!resolved.length) return null;
    let sum = 0;
    for (const g of resolved) sum += ((g.p ?? 0) - (g.outcome ? 1 : 0)) ** 2;
    return { score: sum / resolved.length, count: resolved.length };
  }

  /**
   * One bin per tenth: how many guesses, how many came true, their mean
   * probability and the share that came true.
   * @param {LedgerGuess[]} guesses
   * @returns {LedgerBin[]}
   */
  function calibration(guesses) {
    const bins = [];
    for (let index = 0; index < BINS; index += 1) {
      bins.push({ index, lo: index / BINS, hi: (index + 1) / BINS, count: 0, hits: 0, sum: 0 });
    }
    for (const g of scored(guesses)) {
      const bin = bins[binIndex(g.p ?? 0)];
      bin.count += 1;
      bin.sum += g.p ?? 0;
      if (g.outcome) bin.hits += 1;
    }
    return bins.map(({ sum, ...bin }) => ({
      ...bin,
      meanP: bin.count ? sum / bin.count : null,
      observed: bin.count ? bin.hits / bin.count : null,
    }));
  }

  /** @param {number | null} p */
  function percent(p) {
    if (p === null) return '–';
    return `${Math.round(p * 1000) / 10}%`;
  }

  /** @param {() => number} [random] */
  function newId(random = Math.random) {
    let id = '';
    for (let i = 0; i < 8; i += 1) {
      id += ID_ALPHABET[Math.floor(random() * ID_ALPHABET.length) % ID_ALPHABET.length];
    }
    return id;
  }

  /** @type {any} */ (globalThis).LedgerModel = Object.freeze({
    guessKey,
    parseProbability,
    parseDate,
    parseOutcome,
    parseGuess,
    fromStorage,
    localDay,
    isDue,
    sections,
    addGuess,
    updateGuess,
    resolveGuess,
    removeGuess,
    binIndex,
    brierScore,
    calibration,
    percent,
    newId,
  });
})();
