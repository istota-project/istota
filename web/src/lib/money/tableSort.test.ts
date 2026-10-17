import { describe, expect, it } from 'vitest';
import { nextSort, sortRows } from './tableSort';

describe('nextSort', () => {
  it('flips the active column and starts a new one newest first', () => {
    expect(nextSort({ key: 'date', descending: true }, 'date')).toEqual({
      key: 'date',
      descending: false,
    });
    expect(nextSort({ key: 'date', descending: false }, 'invoice')).toEqual({
      key: 'invoice',
      descending: true,
    });
  });
});

describe('sortRows', () => {
  const rows = ['INV-000010', '', 'INV-000009', 'INV-000100'];

  it('compares the digits in a number as numbers', () => {
    expect(sortRows(rows, false, (r) => [r])).toEqual([
      'INV-000009',
      'INV-000010',
      'INV-000100',
      '',
    ]);
  });

  it('keeps an empty first value last in both directions', () => {
    expect(sortRows(rows, true, (r) => [r])).toEqual([
      'INV-000100',
      'INV-000010',
      'INV-000009',
      '',
    ]);
  });

  it('breaks ties on the later values', () => {
    const tied = [
      { n: 'A', d: '2026-01-01' },
      { n: 'A', d: '2026-03-01' },
    ];
    expect(sortRows(tied, true, (r) => [r.n, r.d]).map((r) => r.d)).toEqual([
      '2026-03-01',
      '2026-01-01',
    ]);
  });
});
