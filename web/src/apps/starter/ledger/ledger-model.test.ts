import { describe, expect, it } from 'vitest';
import { loadStarterScript } from '../../testing/classicScript';

type Guess = {
  id: string;
  text: string;
  p: number | null;
  resolves_on: string | null;
  outcome: boolean | null;
  created_at: string;
  resolved_at: string | null;
  [extra: string]: unknown;
};
type Change = { guesses: Guess[]; changed: Guess };
type Bin = {
  index: number;
  lo: number;
  hi: number;
  count: number;
  hits: number;
  meanP: number | null;
  observed: number | null;
};

type Model = {
  guessKey(id: string): string;
  parseProbability(value: unknown): number | null;
  parseDate(value: unknown): string | null;
  parseOutcome(value: unknown): boolean | null;
  parseGuess(key: string, value: unknown): Guess | null;
  fromStorage(items: Array<{ key: string; value: unknown }>): Guess[];
  localDay(date: Date): string;
  isDue(guess: Guess, today: string): boolean;
  sections(guesses: Guess[], today: string): { due: Guess[]; open: Guess[]; resolved: Guess[] };
  addGuess(
    guesses: Guess[],
    fields: { text: string; p: unknown; resolves_on: unknown },
    now: string,
    id: string,
  ): Change;
  updateGuess(guesses: Guess[], id: string, patch: Partial<Guess>): Change | null;
  resolveGuess(guesses: Guess[], id: string, outcome: boolean | null, now: string): Change | null;
  removeGuess(guesses: Guess[], id: string): Guess[];
  binIndex(p: number): number;
  brierScore(guesses: Guess[]): { score: number; count: number } | null;
  calibration(guesses: Guess[]): Bin[];
  percent(p: number | null): string;
  newId(random?: () => number): string;
};

const NOW = '2026-10-25T12:00:00.000Z';
const LATER = '2026-11-02T09:00:00.000Z';

function model(): Model {
  return loadStarterScript('ledger', 'ledger-model.js').LedgerModel as Model;
}

function item(id: string, value: Record<string, unknown>) {
  return { key: `guess:${id}`, value: { id, text: id, created_at: NOW, ...value } };
}

// A fixed set of resolved guesses with a hand-computed Brier score and bins.
//   p     outcome  (p - o)^2
//   0.9   1        0.01
//   0.9   0        0.81
//   0.7   1        0.09
//   0.6   1        0.16
//   0.3   0        0.09
//   0.05  0        0.0025
//   1.0   1        0
// sum 1.1625 over 7 = 0.166071428...
function fixed(m: Model): Guess[] {
  return m.fromStorage([
    item('a', { p: 0.9, outcome: true, resolves_on: '2026-10-01' }),
    item('b', { p: 0.9, outcome: false, resolves_on: '2026-10-02' }),
    item('c', { p: 0.7, outcome: true, resolves_on: '2026-10-03' }),
    item('d', { p: 0.6, outcome: true, resolves_on: '2026-10-04' }),
    item('e', { p: 0.3, outcome: false, resolves_on: '2026-10-05' }),
    item('f', { p: 0.05, outcome: false, resolves_on: '2026-10-06' }),
    item('g', { p: 1, outcome: true, resolves_on: '2026-10-07' }),
    // Not scored: open, and one with no probability.
    item('h', { p: 0.4, outcome: null, resolves_on: '2026-12-01' }),
    item('i', { outcome: true, resolves_on: '2026-10-08' }),
  ]);
}

describe('reading app.ledger', () => {
  it('reads a probability as a fraction, a percentage written by the agent included', () => {
    const m = model();
    expect(m.parseProbability(0.6)).toBe(0.6);
    expect(m.parseProbability('0.25')).toBe(0.25);
    expect(m.parseProbability(60)).toBe(0.6);
    expect(m.parseProbability(0)).toBe(0);
    expect(m.parseProbability(1)).toBe(1);
    for (const bad of [-0.1, 101, NaN, 'sixty', null, undefined, true]) {
      expect(m.parseProbability(bad)).toBeNull();
    }
  });

  it('reads a resolution date as a real calendar day', () => {
    const m = model();
    expect(m.parseDate('2026-11-01')).toBe('2026-11-01');
    expect(m.parseDate('2026-11-01T08:00:00Z')).toBe('2026-11-01');
    for (const bad of ['2026-02-30', '2026-13-01', '1 Nov', '', null, 20261101]) {
      expect(m.parseDate(bad)).toBeNull();
    }
  });

  it('reads an outcome as true, false or not yet known', () => {
    const m = model();
    expect([true, false, 1, 0, null, 'yes', undefined].map((v) => m.parseOutcome(v))).toEqual([
      true,
      false,
      true,
      false,
      null,
      null,
      null,
    ]);
  });

  it('normalizes what the agent wrote and keeps fields it does not know', () => {
    const m = model();
    const guess = m.parseGuess('guess:x1', { text: 'Rain', p: 70, note: 'from chat', id: 'other' });
    expect(guess).toEqual({
      id: 'x1',
      text: 'Rain',
      p: 0.7,
      resolves_on: null,
      outcome: null,
      created_at: '',
      resolved_at: null,
      note: 'from chat',
    });
    expect(m.parseGuess('card:x1', { text: 'Rain' })).toBeNull();
    expect(m.parseGuess('guess:', { text: 'Rain' })).toBeNull();
    expect(m.parseGuess('guess:x1', 'Rain')).toBeNull();
    expect(m.fromStorage([{ key: 'guess:y', value: [1] }, item('z', {})]).map((g) => g.id)).toEqual(
      ['z'],
    );
  });
});

describe('the resolution prompts', () => {
  it('asks about a guess once its day has come, and not before', () => {
    const m = model();
    const guess = m.parseGuess('guess:a', { text: 'x', p: 0.5, resolves_on: '2026-10-25' })!;
    expect(m.isDue(guess, '2026-10-24')).toBe(false);
    expect(m.isDue(guess, '2026-10-25')).toBe(true);
    expect(m.isDue({ ...guess, outcome: false }, '2026-10-26')).toBe(false);
    expect(m.isDue({ ...guess, resolves_on: null }, '2026-10-26')).toBe(false);
  });

  it('splits guesses into due, open and resolved, each in the order the page shows', () => {
    const m = model();
    const guesses = m.fromStorage([
      item('late', { p: 0.5, resolves_on: '2026-12-01' }),
      item('undated', { p: 0.5 }),
      item('soon', { p: 0.5, resolves_on: '2026-11-01' }),
      item('due2', { p: 0.5, resolves_on: '2026-10-20' }),
      item('due1', { p: 0.5, resolves_on: '2026-10-10' }),
      item('old', { p: 0.5, outcome: true, resolved_at: '2026-09-01T00:00:00Z' }),
      item('new', { p: 0.5, outcome: false, resolved_at: '2026-10-01T00:00:00Z' }),
    ]);
    const { due, open, resolved } = m.sections(guesses, '2026-10-25');
    expect(due.map((g) => g.id)).toEqual(['due1', 'due2']);
    expect(open.map((g) => g.id)).toEqual(['soon', 'late', 'undated']);
    expect(resolved.map((g) => g.id)).toEqual(['new', 'old']);
  });

  it('takes the local calendar day as today', () => {
    expect(model().localDay(new Date(2026, 0, 5, 23, 30))).toBe('2026-01-05');
  });
});

describe('changing guesses', () => {
  it('adds a guess with the stored shape', () => {
    const m = model();
    const { guesses, changed } = m.addGuess(
      [],
      { text: '  The vote passes ', p: '60', resolves_on: '2026-11-04' },
      NOW,
      'n1',
    );
    expect(changed).toEqual({
      id: 'n1',
      text: 'The vote passes',
      p: 0.6,
      resolves_on: '2026-11-04',
      outcome: null,
      created_at: NOW,
      resolved_at: null,
    });
    expect(guesses).toEqual([changed]);
  });

  it('resolves and reopens a guess, leaving the list it was given alone', () => {
    const m = model();
    const before = fixed(m);
    const resolved = m.resolveGuess(before, 'h', false, LATER)!;
    expect(resolved.changed).toMatchObject({ id: 'h', outcome: false, resolved_at: LATER });
    expect(before.find((g) => g.id === 'h')!.outcome).toBeNull();
    const reopened = m.resolveGuess(resolved.guesses, 'h', null, LATER)!;
    expect(reopened.changed).toMatchObject({ outcome: null, resolved_at: null });
    expect(m.resolveGuess(before, 'nope', true, LATER)).toBeNull();
  });

  it('edits text, probability and date through the same normalization', () => {
    const m = model();
    const edited = m.updateGuess(fixed(m), 'h', {
      text: ' Snow ',
      p: 80 as unknown as number,
      resolves_on: 'soon',
    })!;
    expect(edited.changed).toMatchObject({ text: 'Snow', p: 0.8, resolves_on: null });
    expect(m.updateGuess(fixed(m), 'nope', { text: 'x' })).toBeNull();
  });

  it('removes a guess', () => {
    const m = model();
    expect(m.removeGuess(fixed(m), 'a').map((g) => g.id)).not.toContain('a');
  });
});

describe('the Brier score and the calibration bins', () => {
  it('is the mean squared error over resolved guesses with a probability', () => {
    const m = model();
    const brier = m.brierScore(fixed(m))!;
    expect(brier.count).toBe(7);
    expect(brier.score).toBeCloseTo(1.1625 / 7, 12);
  });

  it('is absent with nothing resolved', () => {
    const m = model();
    expect(m.brierScore(m.fromStorage([item('h', { p: 0.4 })]))).toBeNull();
  });

  it('puts a probability in its tenth, with 100% in the top bin', () => {
    const m = model();
    expect([0, 0.05, 0.1, 0.3, 0.6, 0.7, 0.9, 0.99, 1].map((p) => m.binIndex(p))).toEqual([
      0, 0, 1, 3, 6, 7, 9, 9, 9,
    ]);
  });

  it('counts each bin and its observed frequency', () => {
    const m = model();
    const bins = m.calibration(fixed(m));
    expect(bins).toHaveLength(10);
    expect(bins.map((b) => b.count)).toEqual([1, 0, 0, 1, 0, 0, 1, 1, 0, 3]);
    expect(bins[0]).toMatchObject({ lo: 0, hi: 0.1, hits: 0, observed: 0 });
    expect(bins[0].meanP).toBeCloseTo(0.05, 12);
    expect(bins[1]).toMatchObject({ count: 0, meanP: null, observed: null });
    expect(bins[9].hits).toBe(2);
    expect(bins[9].meanP).toBeCloseTo(2.8 / 3, 12);
    expect(bins[9].observed).toBeCloseTo(2 / 3, 12);
  });
});

describe('display', () => {
  it('shows a probability as a percentage', () => {
    const m = model();
    expect([0.6, 0.625, 1, 0, null].map((p) => m.percent(p))).toEqual([
      '60%',
      '62.5%',
      '100%',
      '0%',
      '–',
    ]);
  });

  it('makes ids from its alphabet', () => {
    const m = model();
    expect(m.newId(() => 0)).toBe('00000000');
    expect(m.newId()).toMatch(/^[0-9a-z]{8}$/);
  });
});
