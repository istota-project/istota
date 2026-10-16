import { describe, expect, it } from 'vitest';
import { fillSpans, type FillItem } from './cardFill';

const card = (fill = false, width: FillItem['width'] = 'auto'): FillItem => ({ width, fill });

describe('fill cards widen into the empty tracks after them', () => {
  it('grows a fill card left alone in its row by a full-width card after it', () => {
    // The admin status page at two columns: Task activity sits alone before Users.
    expect(fillSpans([card(), card(), card(true), card(false, 'full')], 2)).toEqual([1, 1, 2, 2]);
  });

  it('keeps a fill card at one column when its row is already full', () => {
    expect(fillSpans([card(), card(), card(true), card(false, 'full')], 3)).toEqual([1, 1, 1, 3]);
  });

  it('grows a fill card that ends the grid with room to its right', () => {
    expect(fillSpans([card(), card(), card(), card(true)], 3)).toEqual([1, 1, 1, 3]);
  });

  it('leaves a card without the flag alone, even with room beside it', () => {
    expect(fillSpans([card(), card(false, 'full')], 3)).toEqual([1, 3]);
  });

  it('fills only up to the card that wrapped, not past a card still in the row', () => {
    // In two columns a wide card after a fill card does not fit and wraps,
    // leaving the fill card its row; in three it fits beside it.
    expect(fillSpans([card(true), card(false, 'wide')], 2)).toEqual([2, 2]);
    expect(fillSpans([card(true), card(false, 'wide')], 3)).toEqual([1, 2]);
    // A card between them keeps the row: the fill card is not the last in it.
    expect(fillSpans([card(true), card(), card(false, 'wide')], 3)).toEqual([1, 1, 2]);
  });

  it('spans every column on a one-column grid without growing anything', () => {
    expect(fillSpans([card(true), card(false, 'wide')], 1)).toEqual([1, 1]);
  });
});
