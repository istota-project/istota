import { describe, expect, it } from 'vitest';
import { loadStarterScript } from '../../testing/classicScript';

type Card = {
  id: string;
  q: string;
  a: string;
  source_path: string | null;
  source_anchor: string | null;
  created_at: string;
  [extra: string]: unknown;
};
type Review = { due: string | null; interval: number; reps: number; [extra: string]: unknown };

type Model = {
  cardKey(id: string): string;
  reviewKey(id: string): string;
  parseCard(key: string, value: unknown): Card | null;
  fromStorage(items: Array<{ key: string; value: unknown }>): {
    cards: Card[];
    reviews: Record<string, Review>;
  };
  queue(cards: Card[], reviews: Record<string, Review>, today: string): Card[];
  nextDue(cards: Card[], reviews: Record<string, Review>, today: string): string | null;
  sourceCandidates(path: unknown): string[];
  findPassage(text: string, anchor: string | null): string | null;
  addCard(fields: { q: string; a: string }, now: string, id: string): Card;
  newId(random?: () => number): string;
};

function model(): Model {
  return loadStarterScript('recall', 'sm2.js', 'recall-model.js').RecallModel as Model;
}

const NOW = '2026-10-25T12:00:00.000Z';

describe('reading app.recall', () => {
  it('splits cards from review state, normalizing each', () => {
    const M = model();
    const { cards, reviews } = M.fromStorage([
      {
        key: 'card:c1',
        value: {
          id: 'other',
          q: 'What is a METAR?',
          a: 'A routine aviation weather report.',
          source_path: 'notes/aviation/weather.md',
          source_anchor: 'METAR',
          created_at: NOW,
          deck: 'aviation',
        },
      },
      { key: 'card:c2', value: { q: 'Only a question' } },
      { key: 'card:', value: { q: 'no id' } },
      { key: 'card:c3', value: 'not an object' },
      { key: 'review:c1', value: { due: '2026-10-30', interval: 6, reps: 2 } },
      { key: 'review:gone', value: { due: '2026-10-30' } },
      { key: 'settings', value: {} },
    ]);
    expect(cards.map((c) => c.id)).toEqual(['c1', 'c2']);
    // The key wins over the id inside the value, and unknown fields are kept.
    expect(cards[0]).toMatchObject({ id: 'c1', deck: 'aviation', source_anchor: 'METAR' });
    expect(cards[1]).toMatchObject({ a: '', source_path: null, source_anchor: null });
    expect(Object.keys(reviews).sort()).toEqual(['c1', 'gone']);
    expect(reviews.c1).toMatchObject({ due: '2026-10-30', interval: 6, reps: 2, ease: 2.5 });
  });

  it('refuses a card with no question', () => {
    const M = model();
    expect(M.parseCard('card:x', { a: 'answer' })).toBeNull();
    expect(M.parseCard('card:x', { q: '   ' })).toBeNull();
  });
});

describe('the review queue', () => {
  const cards = (M: Model) =>
    M.fromStorage([
      { key: 'card:new-late', value: { q: 'n2', created_at: '2026-10-02T00:00:00Z' } },
      { key: 'card:new-early', value: { q: 'n1', created_at: '2026-10-01T00:00:00Z' } },
      { key: 'card:overdue', value: { q: 'o', created_at: '2026-09-01T00:00:00Z' } },
      { key: 'card:today', value: { q: 't', created_at: '2026-09-02T00:00:00Z' } },
      { key: 'card:later', value: { q: 'l', created_at: '2026-09-03T00:00:00Z' } },
      { key: 'review:overdue', value: { due: '2026-10-20', interval: 6, reps: 2 } },
      { key: 'review:today', value: { due: '2026-10-25', interval: 1, reps: 1 } },
      { key: 'review:later', value: { due: '2026-11-03', interval: 15, reps: 3 } },
    ]);

  it('puts due cards first, oldest due first, then new cards oldest first', () => {
    const M = model();
    const { cards: list, reviews } = cards(M);
    expect(M.queue(list, reviews, '2026-10-25').map((c) => c.id)).toEqual([
      'overdue',
      'today',
      'new-early',
      'new-late',
    ]);
  });

  it('names the next day something is due once the queue is empty', () => {
    const M = model();
    const { cards: list, reviews } = cards(M);
    expect(M.nextDue(list, reviews, '2026-10-25')).toBe('2026-11-03');
    expect(M.nextDue([], {}, '2026-10-25')).toBeNull();
  });
});

describe('finding the source', () => {
  it('tries the path as written, then without each leading folder', () => {
    const M = model();
    expect(M.sourceCandidates('notes/aviation/weather.md')).toEqual([
      'notes/aviation/weather.md',
      'aviation/weather.md',
      'weather.md',
    ]);
    expect(M.sourceCandidates('/notes/weather.md')).toEqual(['notes/weather.md', 'weather.md']);
  });

  it('offers nothing for a path that could leave the folder', () => {
    const M = model();
    for (const path of ['', '../x.md', 'a/../b.md', 'a/./b.md', '.hidden/x.md', 'a//b', null, 7]) {
      expect(M.sourceCandidates(path)).toEqual([]);
    }
  });

  const NOTE = [
    '# Aviation weather',
    '',
    'Weather reports come in two kinds.',
    '',
    '## METAR',
    '',
    'A METAR is a routine report,',
    'issued every hour at most airports.',
    '',
    '## TAF',
    '',
    'A TAF is a forecast for the next 24 to 30 hours.',
  ].join('\n');

  it('finds the paragraph holding the anchor text', () => {
    const M = model();
    expect(M.findPassage(NOTE, 'next 24 to   30 HOURS')).toBe(
      'A TAF is a forecast for the next 24 to 30 hours.',
    );
  });

  it('takes a heading with the paragraph under it', () => {
    const M = model();
    expect(M.findPassage(NOTE, '## METAR')).toBe(
      '## METAR\n\nA METAR is a routine report,\nissued every hour at most airports.',
    );
    expect(M.findPassage(NOTE, 'metar')).toBe(
      '## METAR\n\nA METAR is a routine report,\nissued every hour at most airports.',
    );
  });

  it('finds nothing without an anchor, or for one the file does not hold', () => {
    const M = model();
    expect(M.findPassage(NOTE, null)).toBeNull();
    expect(M.findPassage(NOTE, 'volcanic ash')).toBeNull();
  });

  it('reads Windows line endings', () => {
    const M = model();
    expect(M.findPassage('one\r\n\r\ntwo words\r\nmore', 'two')).toBe('two words\nmore');
  });
});

describe('adding a card', () => {
  it('trims the fields and leaves the source empty', () => {
    const M = model();
    expect(M.addCard({ q: '  Q?  ', a: ' A ' }, NOW, 'abc')).toEqual({
      id: 'abc',
      q: 'Q?',
      a: 'A',
      source_path: null,
      source_anchor: null,
      created_at: NOW,
    });
    expect(M.newId(() => 0.5)).toMatch(/^[0-9a-z]{8}$/);
  });
});
