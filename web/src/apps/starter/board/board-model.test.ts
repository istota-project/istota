import { describe, expect, it } from 'vitest';
import { loadStarterScript } from '../../testing/classicScript';

type Card = {
  id: string;
  column: string;
  order: number | null;
  title: string;
  body: string;
  struck: boolean;
  color: string | null;
  updated_at: string;
  [extra: string]: unknown;
};
type Column = { id: string; title: string; synthetic?: boolean };
type State = { columns: Column[]; cards: Card[] };
type Change = { state: State; changed: Card[] };

type Model = {
  COLORS: readonly string[];
  UNSORTED: string;
  cardKey(id: string): string;
  parseColumns(value: unknown): Column[];
  parseCard(key: string, value: unknown): Card | null;
  fromStorage(columns: unknown, items: Array<{ key: string; value: unknown }>): State;
  visibleColumns(state: State): Column[];
  columnCards(state: State, column: string): Card[];
  addCard(state: State, fields: { column: string; title: string }, now: string, id: string): Change;
  updateCard(state: State, id: string, patch: Partial<Card>, now: string): Change;
  removeCard(state: State, id: string): State;
  moveCard(state: State, id: string, column: string, index: number, now: string): Change;
  moveCardBy(state: State, id: string, delta: number, now: string): Change;
  addColumn(state: State, title: string, id: string): State;
  renameColumn(state: State, id: string, title: string): State;
  shiftColumn(state: State, id: string, delta: number): State;
  removeColumn(state: State, id: string): State | null;
  dropIndex(midpoints: number[], y: number): number;
  pressIntent(press: { pointerType: string; elapsed: number; moved: number }): string;
  newId(random?: () => number): string;
};

const NOW = '2026-10-25T12:00:00.000Z';
const LATER = '2026-10-25T12:05:00.000Z';

function model(): Model {
  return loadStarterScript('board', 'board-model.js').BoardModel as Model;
}

function card(id: string, column: string, order: number | null, extra: Partial<Card> = {}) {
  return { key: `card:${id}`, value: { id, column, order, title: id.toUpperCase(), ...extra } };
}

function board(m: Model): State {
  return m.fromStorage(
    [
      { id: 'a', title: 'Options' },
      { id: 'b', title: 'Shortlist' },
    ],
    [card('one', 'a', 1), card('two', 'a', 2), card('three', 'a', 3), card('four', 'b', 1)],
  );
}

function ids(cards: Card[]): string[] {
  return cards.map((c) => c.id);
}

describe('board-model.js', () => {
  it('defines one global and nothing else', () => {
    const globals = loadStarterScript('board', 'board-model.js');
    expect(Object.keys(globals)).toEqual(['BoardModel']);
  });

  describe('reading app.board', () => {
    it('keeps valid columns in order and drops the rest', () => {
      const m = model();
      expect(
        m.parseColumns([
          { id: 'a', title: 'Options' },
          { id: 'a', title: 'Duplicate' },
          { id: '', title: 'No id' },
          'text',
          { id: 'b' },
        ]),
      ).toEqual([
        { id: 'a', title: 'Options' },
        { id: 'b', title: '' },
      ]);
      expect(m.parseColumns(null)).toEqual([]);
      expect(m.parseColumns({ id: 'a' })).toEqual([]);
    });

    it('normalizes a card the agent wrote with only a title, keeping its other fields', () => {
      const m = model();
      const parsed = m.parseCard('card:flat-1', { title: 'Flat on Elm St', rent: 1450 });
      expect(parsed).toEqual({
        id: 'flat-1',
        column: '',
        order: null,
        title: 'Flat on Elm St',
        body: '',
        struck: false,
        color: null,
        updated_at: '',
        rent: 1450,
      });
    });

    it('takes the id from the key and refuses what is not a card', () => {
      const m = model();
      expect(m.parseCard('card:x', { id: 'y', title: 't' })?.id).toBe('x');
      expect(m.parseCard('columns', { title: 't' })).toBeNull();
      expect(m.parseCard('card:', { title: 't' })).toBeNull();
      expect(m.parseCard('card:x', 'just text')).toBeNull();
      expect(m.parseCard('card:x', { title: 't', color: 'magenta', struck: 'yes' })).toMatchObject({
        color: null,
        struck: false,
      });
      expect(m.parseCard('card:x', { title: 't', color: 'teal', order: Infinity })).toMatchObject({
        color: 'teal',
        order: null,
      });
    });

    it('sorts by order, then puts unordered cards last by time and id', () => {
      const m = model();
      const state = m.fromStorage(
        [{ id: 'a', title: 'A' }],
        [
          card('late', 'a', null, { updated_at: LATER }),
          card('early', 'a', null, { updated_at: NOW }),
          card('second', 'a', 2),
          card('first', 'a', 1),
        ],
      );
      expect(ids(m.columnCards(state, 'a'))).toEqual(['first', 'second', 'early', 'late']);
    });

    it('shows cards whose column does not exist in an Unsorted column', () => {
      const m = model();
      const state = m.fromStorage(
        [{ id: 'a', title: 'A' }],
        [card('one', 'a', 1), card('lost', 'gone', 1)],
      );
      const columns = m.visibleColumns(state);
      expect(columns.map((c) => c.id)).toEqual(['a', m.UNSORTED]);
      expect(columns[1]).toMatchObject({ title: 'Unsorted', synthetic: true });
      expect(ids(m.columnCards(state, m.UNSORTED))).toEqual(['lost']);
      expect(m.visibleColumns(m.fromStorage([{ id: 'a', title: 'A' }], []))).toHaveLength(1);
    });
  });

  describe('moving cards', () => {
    it('moves a card between two others with one write', () => {
      const m = model();
      const { state, changed } = m.moveCard(board(m), 'four', 'a', 1, NOW);
      expect(ids(m.columnCards(state, 'a'))).toEqual(['one', 'four', 'two', 'three']);
      expect(m.columnCards(state, 'b')).toEqual([]);
      expect(changed).toHaveLength(1);
      expect(changed[0]).toMatchObject({ id: 'four', column: 'a', order: 1.5, updated_at: NOW });
    });

    it('moves a card to the top, to the end, and into an empty column', () => {
      const m = model();
      let { state } = m.moveCard(board(m), 'three', 'a', 0, NOW);
      expect(ids(m.columnCards(state, 'a'))).toEqual(['three', 'one', 'two']);
      ({ state } = m.moveCard(state, 'three', 'a', 99, NOW));
      expect(ids(m.columnCards(state, 'a'))).toEqual(['one', 'two', 'three']);
      state = m.addColumn(state, 'Empty', 'c');
      const moved = m.moveCard(state, 'one', 'c', 0, NOW);
      expect(ids(m.columnCards(moved.state, 'c'))).toEqual(['one']);
      expect(moved.changed).toHaveLength(1);
    });

    it('renumbers the column when there is no room left between two cards', () => {
      const m = model();
      const state = m.fromStorage(
        [{ id: 'a', title: 'A' }],
        [card('x', 'a', 1), card('y', 'a', 1 + 1e-9), card('z', 'a', 5)],
      );
      const { state: next, changed } = m.moveCard(state, 'z', 'a', 1, NOW);
      expect(ids(m.columnCards(next, 'a'))).toEqual(['x', 'z', 'y']);
      expect(m.columnCards(next, 'a').map((c) => c.order)).toEqual([1, 2, 3]);
      expect(ids(changed).sort()).toEqual(['y', 'z']);
    });

    it('gives unordered neighbours an order when a card lands among them', () => {
      const m = model();
      const state = m.fromStorage(
        [{ id: 'a', title: 'A' }],
        [
          card('p', 'a', null, { updated_at: NOW }),
          card('q', 'a', null, { updated_at: LATER }),
          card('r', 'a', 1),
        ],
      );
      const { state: next, changed } = m.moveCard(state, 'r', 'a', 1, LATER);
      expect(ids(m.columnCards(next, 'a'))).toEqual(['p', 'r', 'q']);
      expect(m.columnCards(next, 'a').every((c) => typeof c.order === 'number')).toBe(true);
      expect(ids(changed).sort()).toEqual(['p', 'q', 'r']);
    });

    it('moving to where a card already is writes nothing', () => {
      const m = model();
      const before = board(m);
      const { state, changed } = m.moveCard(before, 'two', 'a', 1, NOW);
      expect(changed).toEqual([]);
      expect(ids(m.columnCards(state, 'a'))).toEqual(['one', 'two', 'three']);
    });

    it('moves up and down one place for the move menu, and stops at the ends', () => {
      const m = model();
      const up = m.moveCardBy(board(m), 'two', -1, NOW);
      expect(ids(m.columnCards(up.state, 'a'))).toEqual(['two', 'one', 'three']);
      const down = m.moveCardBy(board(m), 'two', 1, NOW);
      expect(ids(m.columnCards(down.state, 'a'))).toEqual(['one', 'three', 'two']);
      expect(m.moveCardBy(board(m), 'one', -1, NOW).changed).toEqual([]);
      expect(m.moveCardBy(board(m), 'three', 1, NOW).changed).toEqual([]);
    });

    it('does not touch the state it was given', () => {
      const m = model();
      const before = board(m);
      const snapshot = JSON.stringify(before);
      m.moveCard(before, 'four', 'a', 0, NOW);
      m.updateCard(before, 'one', { struck: true }, NOW);
      expect(JSON.stringify(before)).toBe(snapshot);
    });
  });

  describe('editing', () => {
    it('adds a card at the end of its column', () => {
      const m = model();
      const { state, changed } = m.addCard(board(m), { column: 'a', title: '  New  ' }, NOW, 'n1');
      expect(ids(m.columnCards(state, 'a'))).toEqual(['one', 'two', 'three', 'n1']);
      expect(changed).toEqual([
        {
          id: 'n1',
          column: 'a',
          order: 4,
          title: 'New',
          body: '',
          struck: false,
          color: null,
          updated_at: NOW,
        },
      ]);
    });

    it('updates the editable fields only, and drops a colour that is not in the palette', () => {
      const m = model();
      const { changed } = m.updateCard(
        board(m),
        'one',
        {
          title: 'Renamed',
          struck: true,
          color: 'neon',
          id: 'other',
          column: 'b',
        } as Partial<Card>,
        NOW,
      );
      expect(changed[0]).toMatchObject({
        id: 'one',
        column: 'a',
        title: 'Renamed',
        struck: true,
        color: null,
        updated_at: NOW,
      });
      expect(m.updateCard(board(m), 'missing', { title: 'x' }, NOW).changed).toEqual([]);
    });

    it('removes a card', () => {
      const m = model();
      expect(ids(m.columnCards(m.removeCard(board(m), 'two'), 'a'))).toEqual(['one', 'three']);
    });

    it('adds, renames and reorders columns', () => {
      const m = model();
      let state = m.addColumn(board(m), ' Decided ', 'c');
      expect(state.columns.map((c) => c.title)).toEqual(['Options', 'Shortlist', 'Decided']);
      state = m.renameColumn(state, 'c', 'Chosen');
      state = m.shiftColumn(state, 'c', -1);
      expect(state.columns.map((c) => c.id)).toEqual(['a', 'c', 'b']);
      expect(m.shiftColumn(state, 'a', -1).columns.map((c) => c.id)).toEqual(['a', 'c', 'b']);
    });

    it('removes only an empty column', () => {
      const m = model();
      expect(m.removeColumn(board(m), 'a')).toBeNull();
      const state = m.addColumn(board(m), 'Empty', 'c');
      expect(m.removeColumn(state, 'c')?.columns.map((c) => c.id)).toEqual(['a', 'b']);
    });

    it('makes ids from the key alphabet', () => {
      const m = model();
      expect(m.newId(() => 0)).toBe('00000000');
      expect(m.newId()).toMatch(/^[0-9a-z]{8}$/);
      expect(m.cardKey('ab12')).toBe('card:ab12');
    });
  });

  describe('dragging', () => {
    it('drops before the first card whose middle is below the pointer', () => {
      const m = model();
      expect(m.dropIndex([10, 50, 90], 0)).toBe(0);
      expect(m.dropIndex([10, 50, 90], 30)).toBe(1);
      expect(m.dropIndex([10, 50, 90], 89)).toBe(2);
      expect(m.dropIndex([10, 50, 90], 200)).toBe(3);
      expect(m.dropIndex([], 5)).toBe(0);
    });

    it('starts a touch drag only on a long press that has not moved', () => {
      const m = model();
      expect(m.pressIntent({ pointerType: 'touch', elapsed: 100, moved: 2 })).toBe('wait');
      expect(m.pressIntent({ pointerType: 'touch', elapsed: 100, moved: 20 })).toBe('cancel');
      expect(m.pressIntent({ pointerType: 'touch', elapsed: 500, moved: 2 })).toBe('drag');
      expect(m.pressIntent({ pointerType: 'pen', elapsed: 500, moved: 2 })).toBe('drag');
    });

    it('starts a mouse drag on movement, without a hold', () => {
      const m = model();
      expect(m.pressIntent({ pointerType: 'mouse', elapsed: 10, moved: 2 })).toBe('wait');
      expect(m.pressIntent({ pointerType: 'mouse', elapsed: 10, moved: 6 })).toBe('drag');
    });
  });
});
