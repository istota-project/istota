import { beforeEach, describe, expect, it } from 'vitest';
import { createTableSort, sortRows } from './tableSort.svelte';

beforeEach(() => localStorage.clear());

describe('createTableSort', () => {
  const keys = ['name', 'size', 'modified'] as const;
  type Key = (typeof keys)[number];
  const fileSort = () =>
    createTableSort<Key>('test.sort', keys, {
      initial: { key: 'name', direction: 'ascending' },
      firstDirection: (key) => (key === 'name' ? 'ascending' : 'descending'),
    });

  it('starts from the initial sort when nothing is remembered', () => {
    const sort = fileSort();
    expect(sort.key).toBe('name');
    expect(sort.direction).toBe('ascending');
    expect(sort.directionOf('name')).toBe('ascending');
    expect(sort.directionOf('size')).toBeNull();
  });

  it('starts a new column at its own first direction and flips the active one', () => {
    const sort = fileSort();
    sort.toggle('modified');
    expect(sort.key).toBe('modified');
    expect(sort.direction).toBe('descending');
    sort.toggle('modified');
    expect(sort.direction).toBe('ascending');
    sort.toggle('name');
    expect(sort.direction).toBe('ascending');
  });

  it('takes one first direction for every column', () => {
    const sort = createTableSort('money.sort', ['date', 'invoice'], {
      initial: { key: 'date', direction: 'descending' },
      firstDirection: 'descending',
    });
    sort.toggle('date');
    expect(sort.descending).toBe(false);
    sort.toggle('invoice');
    expect(sort.descending).toBe(true);
  });

  it('remembers the sort in the shape the earlier copies stored', () => {
    fileSort().toggle('size');
    expect(JSON.parse(localStorage.getItem('test.sort')!)).toEqual({
      key: 'size',
      descending: true,
    });
    const restored = fileSort();
    expect(restored.key).toBe('size');
    expect(restored.direction).toBe('descending');
  });

  it('falls back on a key it does not know, and on a missing direction', () => {
    localStorage.setItem('test.sort', JSON.stringify({ key: 'owner', descending: true }));
    expect(fileSort().key).toBe('name');
    localStorage.setItem('test.sort', JSON.stringify({ key: 'size' }));
    const sort = fileSort();
    expect(sort.key).toBe('size');
    expect(sort.direction).toBe('ascending');
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
