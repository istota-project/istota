import { describe, expect, it } from 'vitest';
import { segments } from './highlight';
describe('search highlights', () => {
  it('merges overlapping and adjacent ranges and clamps to the text', () => {
    expect(
      segments('abcdefgh', [
        [4, 20],
        [1, 3],
        [2, 4],
        [-3, 0],
        [6, 5],
      ]),
    ).toEqual([
      { text: 'a', mark: false },
      { text: 'bcdefgh', mark: true },
    ]);
  });
  it('keeps unmarked text and empty snippets', () => {
    expect(segments('<img onerror>', [])).toEqual([{ text: '<img onerror>', mark: false }]);
    expect(segments('', [[0, 4]])).toEqual([]);
  });
  it('uses Unicode code point offsets from Python', () => {
    expect(segments('😀 cafe', [[2, 6]])).toEqual([
      { text: '😀 ', mark: false },
      { text: 'cafe', mark: true },
    ]);
  });
});
