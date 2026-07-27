import { describe, it, expect } from 'vitest';
import {
  isTap,
  nextActivation,
  UNCHANGED,
  TAP_SLOP_PX,
  TAP_MAX_MS,
  type Activation,
} from './tapActivation';

describe('isTap', () => {
  const start = { x: 100, y: 200, t: 1000 };

  it('accepts a still, brief press', () => {
    expect(isTap(start, { x: 100, y: 200, t: 1080 })).toBe(true);
  });

  it('tolerates the wobble of a real finger', () => {
    expect(isTap(start, { x: 103, y: 204, t: 1120 })).toBe(true);
  });

  it('rejects a scroll flick', () => {
    // The gesture that made an ordinary scroll light up a row: it ends with a
    // pointerup over a message, but the finger travelled to get there.
    expect(isTap(start, { x: 100, y: 200 - 140, t: 1150 })).toBe(false);
  });

  it('rejects movement just past the slop, in either axis', () => {
    expect(isTap(start, { x: 100 + TAP_SLOP_PX + 1, y: 200, t: 1050 })).toBe(false);
    expect(isTap(start, { x: 100, y: 200 - TAP_SLOP_PX - 1, t: 1050 })).toBe(false);
  });

  it('rejects a long press (text selection)', () => {
    expect(isTap(start, { x: 100, y: 200, t: 1000 + TAP_MAX_MS + 1 })).toBe(false);
  });
});

function markup(): HTMLElement {
  const host = document.createElement('div');
  host.innerHTML = `
    <div class="messages">
      <div class="msg" data-cid="7">
        <div class="body"><span id="text">the answer</span></div>
        <button class="star-btn" id="star">star</button>
      </div>
      <div class="msg" data-cid="8"><span id="other">next</span></div>
      <div id="gap"></div>
    </div>`;
  // Deliberately detached: `closest` needs no document, and appending would put
  // duplicate ids in the body — an id selector resolves document-wide before it
  // is filtered to the scope, so the second fixture's lookups would miss.
  return host;
}

describe('nextActivation', () => {
  const pick = (id: string, current: Activation = null) =>
    nextActivation(markup().querySelector(`#${id}`), current);

  it('activates the tapped row', () => {
    expect(pick('text')).toBe(7);
  });

  it('moves activation to another row', () => {
    expect(pick('other', 7)).toBe(8);
  });

  it('clears when the active row is tapped again', () => {
    expect(pick('text', 7)).toBe(null);
  });

  it('clears on a tap that misses every row', () => {
    expect(pick('gap', 7)).toBe(null);
  });

  it('clears on a tap outside the list entirely', () => {
    expect(nextActivation(document.createElement('div'), 7)).toBe(null);
  });

  it('leaves activation alone for a control inside the row', () => {
    // The star's own tap must not pull the row's affordances out from under it.
    expect(pick('star', 7)).toBe(UNCHANGED);
  });

  it('clears on a null target', () => {
    expect(nextActivation(null, 7)).toBe(null);
  });
});
