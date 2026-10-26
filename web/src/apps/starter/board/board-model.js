// Board's pure logic: reading `app.board`, ordering cards, moving and editing
// them. No DOM and no bridge; board.js does the drawing and the writes, and
// board-model.test.ts loads this file on its own.
//
// The data layout in `app.board` (documented in docs/features/apps.md):
//   columns       [{id, title}]
//   card:<id>     {id, column, order, title, body, struck, color, updated_at}
// The agent writes these keys too, through the kv skill, and may leave fields
// out, so every value read is normalized and unknown fields are kept.
//
// Every function returns new objects and leaves the state it was given alone.
// A change returns `changed`: the cards to write back, and nothing else.

/**
 * @typedef {{id: string, title: string, synthetic?: boolean}} BoardColumn
 * @typedef {{id: string, column: string, order: number | null, title: string, body: string,
 *   struck: boolean, color: string | null, updated_at: string, [extra: string]: unknown}} BoardCard
 * @typedef {{columns: BoardColumn[], cards: BoardCard[]}} BoardState
 * @typedef {{state: BoardState, changed: BoardCard[]}} BoardChange
 */
(() => {
  const COLORS = Object.freeze(['sky', 'coral', 'teal', 'plum', 'citron']);
  const UNSORTED = '_unsorted';
  const CARD_PREFIX = 'card:';
  const ID_MAX = 64;
  // Below this gap between two neighbours a new order would not fit between
  // them in a double, so the column is renumbered instead.
  const MIN_GAP = 1e-6;
  const LONG_PRESS_MS = 450;
  const TOUCH_SLOP_PX = 8;
  const MOUSE_SLOP_PX = 4;
  const ID_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyz';

  /** @param {string} id */
  function cardKey(id) {
    return CARD_PREFIX + id;
  }

  /** @param {unknown} value */
  function text(value) {
    return typeof value === 'string' ? value : '';
  }

  /** @param {unknown} value */
  function colorOf(value) {
    return typeof value === 'string' && COLORS.includes(value) ? value : null;
  }

  /**
   * @param {unknown} value
   * @returns {BoardColumn[]}
   */
  function parseColumns(value) {
    if (!Array.isArray(value)) return [];
    const seen = new Set();
    /** @type {BoardColumn[]} */
    const columns = [];
    for (const entry of value) {
      if (!entry || typeof entry !== 'object') continue;
      const id = entry.id;
      if (typeof id !== 'string' || !id || id.length > ID_MAX || seen.has(id)) continue;
      seen.add(id);
      columns.push({ id, title: text(entry.title) });
    }
    return columns;
  }

  /**
   * @param {string} key
   * @param {unknown} raw
   * @returns {BoardCard | null}
   */
  function parseCard(key, raw) {
    if (typeof key !== 'string' || !key.startsWith(CARD_PREFIX)) return null;
    const id = key.slice(CARD_PREFIX.length);
    if (!id || !raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    const value = /** @type {Record<string, unknown>} */ (raw);
    const order = value.order;
    return {
      ...value,
      id,
      column: text(value.column),
      order: typeof order === 'number' && Number.isFinite(order) ? order : null,
      title: text(value.title),
      body: text(value.body),
      struck: value.struck === true,
      color: colorOf(value.color),
      updated_at: text(value.updated_at),
    };
  }

  /**
   * @param {unknown} columns the `columns` value
   * @param {Array<{key: string, value: unknown}>} items the `card:` keys
   * @returns {BoardState}
   */
  function fromStorage(columns, items) {
    /** @type {BoardCard[]} */
    const cards = [];
    for (const item of items || []) {
      const parsed = item ? parseCard(item.key, item.value) : null;
      if (parsed) cards.push(parsed);
    }
    return { columns: parseColumns(columns), cards };
  }

  /**
   * Ordered cards first, by order; then the agent's unordered ones, oldest first.
   * @param {BoardCard} a
   * @param {BoardCard} b
   */
  function compareCards(a, b) {
    if (a.order !== null && b.order !== null && a.order !== b.order) return a.order - b.order;
    if (a.order !== null && b.order === null) return -1;
    if (a.order === null && b.order !== null) return 1;
    if (a.updated_at !== b.updated_at) return a.updated_at < b.updated_at ? -1 : 1;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  }

  /**
   * The column a card is drawn in.
   * @param {BoardState} state
   * @param {BoardCard} card
   */
  function homeOf(state, card) {
    return state.columns.some((c) => c.id === card.column) ? card.column : UNSORTED;
  }

  /**
   * The columns to draw: the stored ones, then Unsorted when a card has no column.
   * @param {BoardState} state
   * @returns {BoardColumn[]}
   */
  function visibleColumns(state) {
    const columns = state.columns.map((c) => ({ ...c }));
    if (state.cards.some((c) => homeOf(state, c) === UNSORTED)) {
      columns.push({ id: UNSORTED, title: 'Unsorted', synthetic: true });
    }
    return columns;
  }

  /**
   * @param {BoardState} state
   * @param {string} column
   */
  function columnCards(state, column) {
    return state.cards.filter((c) => homeOf(state, c) === column).sort(compareCards);
  }

  /**
   * @param {BoardState} state
   * @param {BoardCard[]} changed
   * @returns {BoardState}
   */
  function replaceCards(state, changed) {
    const byId = new Map(changed.map((c) => [c.id, c]));
    const cards = state.cards.map((c) => byId.get(c.id) || c);
    for (const c of changed) if (!state.cards.some((old) => old.id === c.id)) cards.push(c);
    return { columns: state.columns, cards };
  }

  /**
   * Put a card at `index` among the target column's other cards.
   * @param {BoardState} state
   * @param {string} id
   * @param {string} column a column id, or UNSORTED
   * @param {number} index
   * @param {string} now ISO time for updated_at
   * @returns {BoardChange}
   */
  function moveCard(state, id, column, index, now) {
    const moving = state.cards.find((c) => c.id === id);
    if (!moving) return { state, changed: [] };
    const target =
      column === UNSORTED || state.columns.some((c) => c.id === column) ? column : UNSORTED;
    const others = columnCards(state, target).filter((c) => c.id !== id);
    const at = Math.max(0, Math.min(index, others.length));
    if (homeOf(state, moving) === target) {
      const current = columnCards(state, target).findIndex((c) => c.id === id);
      if (current === at) return { state, changed: [] };
    }

    const before = at > 0 ? others[at - 1].order : undefined;
    const after = at < others.length ? others[at].order : undefined;
    const stored =
      target === UNSORTED && homeOf(state, moving) === UNSORTED ? moving.column : target;
    const placed = { ...moving, column: stored, updated_at: now };

    // `undefined` is no neighbour on that side; `null` is a neighbour with no order.
    /** @type {number | null} */
    let order = null;
    if (before !== null && after !== null) {
      if (before !== undefined && after !== undefined) {
        order = after - before >= MIN_GAP ? (before + after) / 2 : null;
      } else if (before !== undefined) order = before + 1;
      else if (after !== undefined) order = after - 1;
      else order = 1;
    }
    if (order !== null) {
      placed.order = order;
      return { state: replaceCards(state, [placed]), changed: [placed] };
    }

    // A neighbour with no order, or no room between two: renumber the column.
    const sequence = [...others.slice(0, at), placed, ...others.slice(at)];
    /** @type {BoardCard[]} */
    const changed = [];
    sequence.forEach((c, position) => {
      const order = position + 1;
      if (c === placed) changed.push({ ...placed, order });
      else if (c.order !== order) changed.push({ ...c, order, updated_at: now });
    });
    return { state: replaceCards(state, changed), changed };
  }

  /**
   * One place up (-1) or down (1) in the card's column; nothing at either end.
   * @param {BoardState} state
   * @param {string} id
   * @param {number} delta
   * @param {string} now
   * @returns {BoardChange}
   */
  function moveCardBy(state, id, delta, now) {
    const moving = state.cards.find((c) => c.id === id);
    if (!moving) return { state, changed: [] };
    const home = homeOf(state, moving);
    const list = columnCards(state, home);
    const target = list.findIndex((c) => c.id === id) + delta;
    if (target < 0 || target >= list.length) return { state, changed: [] };
    return moveCard(state, id, home, target, now);
  }

  /**
   * @param {BoardState} state
   * @param {{column: string, title: string}} fields
   * @param {string} now
   * @param {string} id
   * @returns {BoardChange}
   */
  function addCard(state, fields, now, id) {
    const list = columnCards(state, fields.column);
    const last = list.length ? list[list.length - 1].order : null;
    /** @type {BoardCard} */
    const created = {
      id,
      column: fields.column,
      order: last === null ? list.length + 1 : last + 1,
      title: text(fields.title).trim(),
      body: '',
      struck: false,
      color: null,
      updated_at: now,
    };
    return { state: replaceCards(state, [created]), changed: [created] };
  }

  /**
   * Change a card's title, body, struck or color; nothing else is editable here.
   * @param {BoardState} state
   * @param {string} id
   * @param {Record<string, unknown>} patch
   * @param {string} now
   * @returns {BoardChange}
   */
  function updateCard(state, id, patch, now) {
    const existing = state.cards.find((c) => c.id === id);
    if (!existing) return { state, changed: [] };
    const next = { ...existing, updated_at: now };
    if ('title' in patch) next.title = text(patch.title).trim();
    if ('body' in patch) next.body = text(patch.body);
    if ('struck' in patch) next.struck = patch.struck === true;
    if ('color' in patch) next.color = colorOf(patch.color);
    return { state: replaceCards(state, [next]), changed: [next] };
  }

  /**
   * @param {BoardState} state
   * @param {string} id
   * @returns {BoardState}
   */
  function removeCard(state, id) {
    return { columns: state.columns, cards: state.cards.filter((c) => c.id !== id) };
  }

  /**
   * @param {BoardState} state
   * @param {string} title
   * @param {string} id
   * @returns {BoardState}
   */
  function addColumn(state, title, id) {
    return { columns: [...state.columns, { id, title: text(title).trim() }], cards: state.cards };
  }

  /**
   * @param {BoardState} state
   * @param {string} id
   * @param {string} title
   * @returns {BoardState}
   */
  function renameColumn(state, id, title) {
    const columns = state.columns.map((c) =>
      c.id === id ? { ...c, title: text(title).trim() } : c,
    );
    return { columns, cards: state.cards };
  }

  /**
   * @param {BoardState} state
   * @param {string} id
   * @param {number} delta
   * @returns {BoardState}
   */
  function shiftColumn(state, id, delta) {
    const from = state.columns.findIndex((c) => c.id === id);
    const to = from + delta;
    if (from < 0 || to < 0 || to >= state.columns.length) return state;
    const columns = [...state.columns];
    const [moved] = columns.splice(from, 1);
    columns.splice(to, 0, moved);
    return { columns, cards: state.cards };
  }

  /**
   * A column with cards in it is not removed: returns null.
   * @param {BoardState} state
   * @param {string} id
   * @returns {BoardState | null}
   */
  function removeColumn(state, id) {
    if (columnCards(state, id).length) return null;
    return { columns: state.columns.filter((c) => c.id !== id), cards: state.cards };
  }

  /**
   * Where a dragged card lands: before the first card whose middle is below
   * the pointer. `midpoints` are the other cards' vertical middles, in order.
   * @param {number[]} midpoints
   * @param {number} y
   */
  function dropIndex(midpoints, y) {
    let index = 0;
    while (index < midpoints.length && midpoints[index] < y) index += 1;
    return index;
  }

  /**
   * Whether a press has become a drag. A touch or pen drags only after a
   * still hold, so a swipe still scrolls; moving first cancels it. A mouse
   * drags as soon as it moves.
   * @param {{pointerType: string, elapsed: number, moved: number}} press
   */
  function pressIntent(press) {
    if (press.pointerType === 'mouse') return press.moved >= MOUSE_SLOP_PX ? 'drag' : 'wait';
    if (press.moved > TOUCH_SLOP_PX) return 'cancel';
    return press.elapsed >= LONG_PRESS_MS ? 'drag' : 'wait';
  }

  /** @param {() => number} [random] */
  function newId(random = Math.random) {
    let id = '';
    for (let i = 0; i < 8; i += 1) {
      id += ID_ALPHABET[Math.floor(random() * ID_ALPHABET.length) % ID_ALPHABET.length];
    }
    return id;
  }

  /** @type {any} */ (globalThis).BoardModel = Object.freeze({
    COLORS,
    UNSORTED,
    LONG_PRESS_MS,
    cardKey,
    parseColumns,
    parseCard,
    fromStorage,
    visibleColumns,
    columnCards,
    moveCard,
    moveCardBy,
    addCard,
    updateCard,
    removeCard,
    addColumn,
    renameColumn,
    shiftColumn,
    removeColumn,
    dropIndex,
    pressIntent,
    newId,
  });
})();
