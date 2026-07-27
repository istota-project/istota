/**
 * Tap activation for the message list — the touch surrogate for hover.
 *
 * A touch device has no hover to reveal a row's metadata + star with, and
 * leaning on the synthesized one is what left stars stuck on every row ever
 * tapped (iOS Safari clears its fake `:hover` only when a later tap displaces
 * it). So the list marks a single row active instead, and the rules for what a
 * gesture does to that activation live here as pure functions — the page owns
 * the state, this owns the decisions, and the decisions are testable without
 * standing up the page's stores.
 *
 * The non-obvious half is telling a tap from a scroll. A flick down the list
 * ends with a `pointerup` over a message just as a tap does, so keying off the
 * up event alone made an ordinary scroll light up whichever row the finger
 * happened to leave — which reads exactly like a star that appeared from
 * nowhere and then stuck.
 */

/** Movement past this (CSS px, straight-line) makes the gesture a drag/scroll. */
export const TAP_SLOP_PX = 10;
/** A press held longer than this is a long-press (text selection), not a tap. */
export const TAP_MAX_MS = 600;

export interface PointerSample {
  x: number;
  y: number;
  /** `Date.now()` / `event.timeStamp` — any monotonic ms clock, used as a delta. */
  t: number;
}

/** The active row's cid, or null for "no row active". */
export type Activation = number | null;

/** Returned when a gesture must leave the current activation alone. */
export const UNCHANGED = 'unchanged' as const;

export function isTap(start: PointerSample, end: PointerSample): boolean {
  const moved = Math.hypot(end.x - start.x, end.y - start.y);
  return moved <= TAP_SLOP_PX && end.t - start.t <= TAP_MAX_MS;
}

/**
 * What a tap on `target` does to the activation.
 *
 * - a control (star, room chip, confirm/reject): unchanged — the tap is that
 *   control's, and pulling the row's affordances out from under a button the
 *   user is in the middle of pressing is its own bug;
 * - the active row: cleared, so a second tap dismisses;
 * - another row: that row;
 * - anything else (the list background, or a tap outside the list entirely):
 *   cleared.
 */
export function nextActivation(
  target: Element | null,
  current: Activation,
): Activation | typeof UNCHANGED {
  if (target?.closest('button, a, input, textarea, select')) return UNCHANGED;
  const row = target?.closest<HTMLElement>('[data-cid]');
  const raw = row?.dataset.cid;
  if (raw === undefined) return null;
  const cid = Number(raw);
  if (!Number.isFinite(cid)) return null;
  return cid === current ? null : cid;
}
