/**
 * The zoom transform is pure arithmetic over screen coordinates, so it is
 * tested here rather than through the component: jsdom lays nothing out, and
 * every rect the component would hand these functions comes back zero.
 */
import { describe, it, expect } from 'vitest';
import {
  FIT,
  MAX_SCALE,
  WHEEL_DELTA_CAP,
  applyGesture,
  distance,
  isZoomed,
  midpoint,
  normalizeWheelDelta,
  wheelScaleFactor,
  type Geometry,
} from './imageZoom';

/** A 400x300 image centered in a 1000x800 viewport, untransformed. */
function geometry(over: Partial<Geometry> = {}): Geometry {
  return {
    origin: { x: 500, y: 400 },
    fit: { width: 400, height: 300 },
    viewport: { width: 1000, height: 800 },
    ...over,
  };
}

/** Where the visual center of the image sits, given a state. */
function center(state: { x: number; y: number }, geom = geometry()) {
  return { x: geom.origin.x + state.x, y: geom.origin.y + state.y };
}

describe('distance and midpoint', () => {
  it('measures the gap between two pointers', () => {
    expect(distance({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5);
  });

  it('finds the point between two pointers', () => {
    expect(midpoint({ x: 0, y: 10 }, { x: 4, y: 20 })).toEqual({ x: 2, y: 15 });
  });
});

describe('isZoomed', () => {
  it('is false at the fit scale and true above it', () => {
    expect(isZoomed(FIT)).toBe(false);
    expect(isZoomed({ scale: 1.0001, x: 0, y: 0 })).toBe(true);
  });
});

describe('applyGesture', () => {
  it('scales about the anchor when the anchor is the image center', () => {
    const next = applyGesture(
      FIT,
      { scaleFactor: 2, anchorStart: { x: 500, y: 400 }, anchorNow: { x: 500, y: 400 } },
      geometry(),
    );
    expect(next.scale).toBe(2);
    expect(next.x).toBe(0);
    expect(next.y).toBe(0);
  });

  it('keeps the content under an off-center anchor in place', () => {
    // Pinching above and left of centre has to push the image down and right,
    // so the pixel under the fingers does not slide away. At 4x the image is
    // 1600x1200, with 300px of horizontal slack and 200 vertical, so this
    // anchor is one the pan bounds can actually honour — the next test is the
    // case where they cannot.
    const anchor = { x: 450, y: 380 };
    const next = applyGesture(
      FIT,
      { scaleFactor: 4, anchorStart: anchor, anchorNow: anchor },
      geometry(),
    );
    // c1 = anchor - (anchor - c0) * factor
    expect(center(next)).toEqual({ x: 650, y: 460 });
  });

  it('gives up the anchor rather than the pan bounds when the two disagree', () => {
    // Same gesture, further out: honouring it would need 600px of travel and
    // there are only 300, so the image sits against the edge instead. Holding
    // the anchor here would open a gap between the image and the viewport.
    const anchor = { x: 300, y: 250 };
    const next = applyGesture(
      FIT,
      { scaleFactor: 4, anchorStart: anchor, anchorNow: anchor },
      geometry(),
    );
    expect(next.x).toBe(300);
    expect(next.y).toBe(200);
  });

  it('translates by the anchor delta when nothing is scaled', () => {
    const start = { scale: 3, x: 0, y: 0 };
    const next = applyGesture(
      start,
      { scaleFactor: 1, anchorStart: { x: 500, y: 400 }, anchorNow: { x: 540, y: 370 } },
      geometry(),
    );
    expect(next.scale).toBe(3);
    expect(next.x).toBe(40);
    expect(next.y).toBe(-30);
  });

  it('caps the scale and anchors on the capped factor, not the asked-for one', () => {
    // A two-finger fling can ask for 50x. Anchoring on the raw factor while
    // clamping the scale would throw the image off screen at the cap.
    const anchor = { x: 300, y: 250 };
    const next = applyGesture(
      FIT,
      { scaleFactor: 50, anchorStart: anchor, anchorNow: anchor },
      geometry(),
    );
    expect(next.scale).toBe(MAX_SCALE);
    const capped = applyGesture(
      FIT,
      { scaleFactor: MAX_SCALE, anchorStart: anchor, anchorNow: anchor },
      geometry(),
    );
    expect(next).toEqual(capped);
  });

  it('will not shrink below the fit scale, and re-centers when it lands there', () => {
    const start = { scale: 2, x: 120, y: 80 };
    const next = applyGesture(
      start,
      { scaleFactor: 0.1, anchorStart: { x: 300, y: 250 }, anchorNow: { x: 300, y: 250 } },
      geometry(),
    );
    expect(next).toEqual(FIT);
  });

  it('holds the image against the viewport edge rather than letting it drift off', () => {
    // At 4x the image is 1600x1200 in a 1000x800 viewport, so 300px of slack
    // each way horizontally and 200 vertically.
    const start = { scale: 4, x: 0, y: 0 };
    const next = applyGesture(
      start,
      { scaleFactor: 1, anchorStart: { x: 500, y: 400 }, anchorNow: { x: 5000, y: 5000 } },
      geometry(),
    );
    expect(next.x).toBe(300);
    expect(next.y).toBe(200);
  });

  it('centers an axis whose zoomed image still fits the viewport', () => {
    // 400x300 at 2x is 800x600: narrower than 1000 and shorter than 800, so
    // neither axis has anywhere to pan to.
    const start = { scale: 2, x: 0, y: 0 };
    const next = applyGesture(
      start,
      { scaleFactor: 1, anchorStart: { x: 500, y: 400 }, anchorNow: { x: 800, y: 700 } },
      geometry(),
    );
    expect(next.x).toBe(0);
    expect(next.y).toBe(0);
  });

  it('drifts when fed its own output, which is why callers hold a gesture start', () => {
    // The contract on `applyGesture` is that a gesture is always applied from
    // the state it began in. This is what goes wrong otherwise: run the image
    // into the pan bound and back, one step at a time, and the clamp in the
    // middle has eaten the travel that the return leg needed.
    const geom = geometry();
    const start = { scale: 4, x: 0, y: 0 };
    const out = { x: 5000, y: 400 };
    const back = { x: 500, y: 400 };

    const accumulated = applyGesture(
      applyGesture(start, { scaleFactor: 1, anchorStart: back, anchorNow: out }, geom),
      { scaleFactor: 1, anchorStart: out, anchorNow: back },
      geom,
    );
    const fromStart = applyGesture(
      start,
      { scaleFactor: 1, anchorStart: back, anchorNow: back },
      geom,
    );

    expect(fromStart).toEqual(start);
    expect(accumulated.x).not.toBe(fromStart.x);
  });

  it('survives a geometry with no measured size', () => {
    // jsdom, and the first frame before layout: every rect is zero. The
    // arithmetic must not produce NaN, which would reach the DOM as an
    // unparseable transform and blank the image.
    const next = applyGesture(
      FIT,
      { scaleFactor: 2, anchorStart: { x: 0, y: 0 }, anchorNow: { x: 0, y: 0 } },
      { origin: { x: 0, y: 0 }, fit: { width: 0, height: 0 }, viewport: { width: 0, height: 0 } },
    );
    expect(next).toEqual({ scale: 2, x: 0, y: 0 });
  });
});

describe('wheel deltas', () => {
  it('zooms in on a negative delta and out on a positive one', () => {
    expect(wheelScaleFactor(-10)).toBeGreaterThan(1);
    expect(wheelScaleFactor(10)).toBeLessThan(1);
    expect(wheelScaleFactor(0)).toBe(1);
  });

  it('caps one event, so a coarse notch cannot cross the whole range', () => {
    expect(wheelScaleFactor(-1000)).toBe(wheelScaleFactor(-WHEEL_DELTA_CAP));
    expect(wheelScaleFactor(1000)).toBe(wheelScaleFactor(WHEEL_DELTA_CAP));
  });

  it('converts a delta measured in lines, which is what a mouse wheel reports', () => {
    // Firefox sends about 3 lines per notch. Read as pixels that is a 2.5%
    // step, which puts the far end of the zoom range dozens of notches away.
    const pixels = normalizeWheelDelta(-3, 0, 800);
    const lines = normalizeWheelDelta(-3, 1, 800);
    expect(pixels).toBe(-3);
    expect(lines).toBeLessThan(-40);
    expect(wheelScaleFactor(lines)).toBeGreaterThan(wheelScaleFactor(pixels));
  });

  it('converts a delta measured in pages against the viewport', () => {
    expect(normalizeWheelDelta(-1, 2, 800)).toBe(-800);
  });
});
