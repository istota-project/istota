// Recall's pure logic apart from the scheduling: reading `app.recall`, the
// review queue, and finding a card's source paragraph in its note. No DOM and
// no bridge; recall.js does the drawing, the reads and the writes, and
// recall-model.test.ts loads this file after sm2.js, whose `SM2` it reads.
//
// The data layout in `app.recall` (documented in docs/features/apps.md):
//   card:<id>     {id, q, a, source_path, source_anchor, created_at}
//   review:<id>   {due, interval, ease, reps, lapses, history}   (see sm2.js)
// The agent writes the cards through the kv skill and may leave fields out,
// so every value read is normalized and unknown fields are kept. Recall writes
// only the review keys, and a card it adds itself.

/**
 * @typedef {{id: string, q: string, a: string, source_path: string | null,
 *   source_anchor: string | null, created_at: string, [extra: string]: unknown}} RecallCard
 */
(() => {
  const SM2 = /** @type {any} */ (globalThis).SM2;
  const CARD_PREFIX = 'card:';
  const REVIEW_PREFIX = 'review:';
  const ID_MAX = 64;
  const PASSAGE_MAX = 4000;
  const ID_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyz';

  /** @param {string} id */
  function cardKey(id) {
    return CARD_PREFIX + id;
  }

  /** @param {string} id */
  function reviewKey(id) {
    return REVIEW_PREFIX + id;
  }

  /** @param {unknown} value */
  function text(value) {
    return typeof value === 'string' ? value : '';
  }

  /** @param {unknown} value */
  function textOrNull(value) {
    return typeof value === 'string' && value.trim() ? value.trim() : null;
  }

  /**
   * @param {string} key
   * @param {string} prefix
   */
  function idOf(key, prefix) {
    if (typeof key !== 'string' || !key.startsWith(prefix)) return null;
    const id = key.slice(prefix.length);
    return id && id.length <= ID_MAX ? id : null;
  }

  /**
   * @param {string} key
   * @param {unknown} raw
   * @returns {RecallCard | null}
   */
  function parseCard(key, raw) {
    const id = idOf(key, CARD_PREFIX);
    if (!id || !raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    const value = /** @type {Record<string, unknown>} */ (raw);
    const q = text(value.q).trim();
    if (!q) return null;
    return {
      ...value,
      id,
      q,
      a: text(value.a).trim(),
      source_path: textOrNull(value.source_path),
      source_anchor: textOrNull(value.source_anchor),
      created_at: text(value.created_at),
    };
  }

  /**
   * @param {Array<{key: string, value: unknown}>} items
   * @returns {{cards: RecallCard[], reviews: Record<string, RecallReview>}}
   */
  function fromStorage(items) {
    /** @type {RecallCard[]} */
    const cards = [];
    /** @type {Record<string, RecallReview>} */
    const reviews = {};
    for (const item of Array.isArray(items) ? items : []) {
      if (!item) continue;
      const card = parseCard(item.key, item.value);
      if (card) {
        cards.push(card);
        continue;
      }
      const reviewId = idOf(item.key, REVIEW_PREFIX);
      if (reviewId) reviews[reviewId] = SM2.parseReview(item.value);
    }
    return { cards, reviews };
  }

  /**
   * Due cards (oldest due first), then new ones (oldest first). A card with a
   * review that has no due day counts as new.
   * @param {RecallCard[]} cards
   * @param {Record<string, RecallReview>} reviews
   * @param {string} today
   */
  function queue(cards, reviews, today) {
    /** @type {Array<{card: RecallCard, due: string}>} */
    const due = [];
    /** @type {RecallCard[]} */
    const fresh = [];
    for (const card of cards) {
      const review = reviews[card.id];
      if (!review || !review.due) fresh.push(card);
      else if (review.due <= today) due.push({ card, due: review.due });
    }
    due.sort((a, b) =>
      a.due === b.due ? a.card.created_at.localeCompare(b.card.created_at) : a.due < b.due ? -1 : 1,
    );
    fresh.sort((a, b) => a.created_at.localeCompare(b.created_at));
    return [...due.map((d) => d.card), ...fresh];
  }

  /**
   * The first day after `today` a card comes due, or null.
   * @param {RecallCard[]} cards
   * @param {Record<string, RecallReview>} reviews
   * @param {string} today
   */
  function nextDue(cards, reviews, today) {
    /** @type {string | null} */
    let next = null;
    for (const card of cards) {
      const review = reviews[card.id];
      const due = review ? review.due : null;
      if (due && due > today && (next === null || due < next)) next = due;
    }
    return next;
  }

  /**
   * The paths to try under the granted folder, longest first. A card names its
   * note as a path in the user's files, but the app reads relative to the
   * folder it was granted, which it is not told; so `notes/aviation/x.md` is
   * tried as written, then as `aviation/x.md`, then `x.md`. Nothing is offered
   * for a path with an empty, `.`, `..` or hidden part.
   * @param {unknown} path
   * @returns {string[]}
   */
  function sourceCandidates(path) {
    if (typeof path !== 'string') return [];
    const parts = path.replace(/^\/+/, '').split('/');
    for (const part of parts) {
      if (!part || part === '.' || part === '..' || part.startsWith('.')) return [];
    }
    const candidates = [];
    for (let i = 0; i < parts.length; i += 1) candidates.push(parts.slice(i).join('/'));
    return candidates;
  }

  /** @param {string} value */
  function normalize(value) {
    return value
      .split('\n')
      .map((line) => line.replace(/^\s*#+\s*/, ''))
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase();
  }

  /** @param {string} block */
  function isHeading(block) {
    return block.split('\n').every((line) => /^\s*#/.test(line));
  }

  /**
   * The paragraph of `text` holding `anchor` (matched case- and
   * space-insensitively, `#` marks ignored), or null. A heading comes with the
   * paragraph under it.
   * @param {string} text
   * @param {string | null} anchor
   * @returns {string | null}
   */
  function findPassage(text, anchor) {
    if (typeof text !== 'string' || typeof anchor !== 'string') return null;
    const wanted = normalize(anchor);
    if (!wanted) return null;
    const blocks = text
      .replace(/\r\n?/g, '\n')
      .split(/\n[ \t]*\n/)
      .map((block) => block.trim())
      .filter(Boolean);
    const index = blocks.findIndex((block) => normalize(block).includes(wanted));
    if (index < 0) return null;
    let passage = blocks[index];
    const next = blocks[index + 1];
    if (isHeading(passage) && next && !isHeading(next)) passage = `${passage}\n\n${next}`;
    return passage.length > PASSAGE_MAX ? `${passage.slice(0, PASSAGE_MAX)}…` : passage;
  }

  /**
   * @param {{q: string, a: string}} fields
   * @param {string} now
   * @param {string} id
   * @returns {RecallCard}
   */
  function addCard(fields, now, id) {
    return {
      id,
      q: text(fields.q).trim(),
      a: text(fields.a).trim(),
      source_path: null,
      source_anchor: null,
      created_at: now,
    };
  }

  /** @param {() => number} [random] */
  function newId(random = Math.random) {
    let id = '';
    for (let i = 0; i < 8; i += 1) {
      id += ID_ALPHABET[Math.floor(random() * ID_ALPHABET.length) % ID_ALPHABET.length];
    }
    return id;
  }

  /** @type {any} */ (globalThis).RecallModel = Object.freeze({
    cardKey,
    reviewKey,
    parseCard,
    fromStorage,
    queue,
    nextDue,
    sourceCandidates,
    findPassage,
    addCard,
    newId,
  });
})();
