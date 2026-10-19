import { describe, expect, it } from 'vitest';
import { formatList, parseList, parseNumber, parseOptionalInt } from './parse';

describe('parseNumber', () => {
  it('reads an int or a float, and refuses anything else', () => {
    expect(parseNumber('42', 'int')).toBe(42);
    expect(parseNumber(' 42 ', 'int')).toBe(42);
    expect(parseNumber('4.5', 'int')).toBeNull();
    expect(parseNumber('4.5', 'float')).toBe(4.5);
    expect(parseNumber('', 'float')).toBeNull();
    expect(parseNumber('abc', 'int')).toBeNull();
  });
});

describe('parseOptionalInt', () => {
  it('is null for a blank field, which unsets the setting', () => {
    expect(parseOptionalInt('')).toBeNull();
    expect(parseOptionalInt('   ')).toBeNull();
  });

  it('reads a whole number at or above the minimum', () => {
    expect(parseOptionalInt('0')).toBe(0);
    expect(parseOptionalInt('14')).toBe(14);
    expect(parseOptionalInt('30', { min: 1 })).toBe(30);
  });

  it('is undefined for text that is not a whole number at or above the minimum', () => {
    expect(parseOptionalInt('0', { min: 1 })).toBeUndefined();
    expect(parseOptionalInt('-1')).toBeUndefined();
    expect(parseOptionalInt('2.5')).toBeUndefined();
    expect(parseOptionalInt('soon')).toBeUndefined();
  });
});

describe('parseList', () => {
  it('splits on commas by default, trimming and dropping empties', () => {
    expect(parseList(' a@example.com, ,b@example.com ,')).toEqual([
      'a@example.com',
      'b@example.com',
    ]);
    expect(parseList('')).toEqual([]);
  });

  it('splits one item per line', () => {
    expect(parseList('a\n\n b \n', { separator: '\n' })).toEqual(['a', 'b']);
  });

  it('reads numbers, or null when an item is not one', () => {
    expect(parseList('1\n2', { separator: '\n', numeric: true })).toEqual([1, 2]);
    expect(parseList('1\nx', { separator: '\n', numeric: true })).toBeNull();
  });
});

describe('formatList', () => {
  it('is the comma list parseList reads back', () => {
    expect(formatList(['a', 'b'])).toBe('a, b');
    expect(parseList(formatList(['a', 'b']))).toEqual(['a', 'b']);
  });
});
