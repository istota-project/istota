/**
 * Input modality, and the one behaviour that follows from it.
 *
 * Nothing here is about the native shell — mobile Safari is in exactly the same
 * position — so this ships everywhere and is gated only on whether a soft
 * keyboard is what the device types with.
 */

/**
 * Is a soft keyboard what this device types with?
 *
 * A coarse pointer means no hardware keyboard worth assuming, which is what
 * callers actually want to know: whether taking focus away costs the user a tap
 * (desktop, where they were about to keep typing) or hands them back a third of
 * the screen (a phone).
 */
export function usesSoftKeyboard(): boolean {
  if (typeof window === 'undefined') return false;
  return window.matchMedia?.('(pointer: coarse)').matches === true;
}

/** The composer decides its own keyboard, so taps inside it are left alone. */
const KEEPS_KEYBOARD = '.composer, [data-keeps-keyboard]';

function focusedTextEntry(): HTMLElement | null {
  const el = document.activeElement as HTMLElement | null;
  if (!el) return null;
  const tag = el.tagName;
  const entry = tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable;
  return entry ? el : null;
}

function isTextEntry(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || (el as HTMLElement).isContentEditable === true;
}

/** Movement beyond this is a drag, not a tap that wandered a few pixels. */
const TAP_SLOP_PX = 10;

/**
 * How far down the content has to have come before speed is worth reading.
 * A flick's first samples are the noisiest, and nothing has visibly moved yet.
 */
const MIN_DRAG_PX = 24;

/**
 * Downward speed that reads as putting the keyboard away rather than reading.
 *
 * 0.6px/ms is 600px/s — several times a comfortable reading scroll and well
 * under a flick, so the two do not have to be told apart by how far they went.
 */
const FLICK_PX_PER_MS = 0.6;

/** The one gesture in flight, from the finger going down to it coming off. */
interface Gesture {
  /** The field to blur. Held from the start so the decision can't drift onto
   *  whatever happens to be focused by the time the finger lifts. */
  field: HTMLElement;
  startY: number;
  /** The previous sample, which is what a speed gets measured against. */
  lastY: number;
  lastAt: number;
  /** Past the tap slop — so the lift is the end of a scroll, not a tap. */
  moved: boolean;
  /** Already dismissed; the rest of the drag has nothing left to decide. */
  spent: boolean;
}

/** The y of a pointer or touch event, or null if it carries no position. */
function eventY(e: Event): number | null {
  const touch = (e as TouchEvent).touches?.[0];
  if (touch) return touch.clientY;
  const y = (e as PointerEvent).clientY;
  return typeof y === 'number' ? y : null;
}

/**
 * Dismiss the soft keyboard on a tap outside the composer, and on a deliberate
 * downward drag over the content — but not on the small scroll you make to read
 * back over what you just typed.
 *
 * iOS keeps the keyboard up through a tap on non-focusable content — a
 * paragraph in the transcript is not a focus target, so nothing takes focus off
 * the field and nothing dismisses. In practice that means tapping twice: once
 * for something to happen, once for the keyboard to go. This makes one tap
 * enough.
 *
 * The scroll half is iMessage's rule rather than ours. Blurring the moment a
 * gesture started meant any nudge of the transcript closed the keyboard, and
 * wanting to see the line above what you are writing is not the same as being
 * finished with it. So a gesture is watched rather than acted on.
 *
 * What it is watched for is *speed*, not distance. Reading back through a long
 * exchange is a slow drag that can run the whole height of the screen several
 * times over and still not mean "I'm done typing", so any distance threshold
 * eventually fires on someone who is only reading. A deliberate put-it-away is
 * a flick: fast, and downward, in the pull-the-keyboard-down direction. So a
 * slow drag keeps the keyboard however far it runs, and it goes when the drag
 * speeds up past `FLICK_PX_PER_MS`. Upward — toward the newest message, where
 * the keyboard already is — never dismisses at any speed.
 *
 * Speed is read from the step between two samples rather than across the whole
 * gesture, so slowing down mid-drag stops counting immediately instead of being
 * averaged against how fast it started.
 *
 * What that costs: a tap now dismisses on the lift rather than the press,
 * because until the finger moves (or doesn't) there is nothing to tell a tap
 * from the start of a scroll.
 *
 * `touchmove` is a backstop for a drag that begins somewhere pointerdown was
 * not delivered; with no origin recorded, its first move becomes the origin.
 *
 * Three things are deliberately exempt: the composer and anything marked
 * `data-keeps-keyboard` (a control that acts on the text it is next to), the
 * focused field itself, and any other text entry — moving between two fields is
 * one keyboard, and blurring in between bounces it down and straight back up.
 */
export function installKeyboardDismiss(): () => void {
  if (typeof window === 'undefined') return () => {};

  let gesture: Gesture | null = null;

  /** The field this gesture may dismiss, or null if it may not dismiss at all. */
  function dismissableField(e: Event): HTMLElement | null {
    if (!usesSoftKeyboard()) return null;
    const field = focusedTextEntry();
    if (!field) return null;

    const target = e.target as Element | null;
    if (!target || typeof target.closest !== 'function') return null;
    if (target === field || isTextEntry(target)) return null;
    if (target.closest(KEEPS_KEYBOARD)) return null;

    return field;
  }

  const onDown = (e: Event) => {
    gesture = null;
    const field = dismissableField(e);
    const y = eventY(e);
    if (!field || y === null) return;
    gesture = { field, startY: y, lastY: y, lastAt: Date.now(), moved: false, spent: false };
  };

  const onMove = (e: Event) => {
    const y = eventY(e);
    if (y === null) return;
    const now = Date.now();

    if (!gesture) {
      // A drag whose pointerdown never arrived. Take this move as the origin;
      // the next one can act on it.
      const field = dismissableField(e);
      if (!field) return;
      gesture = { field, startY: y, lastY: y, lastAt: now, moved: true, spent: false };
      return;
    }
    if (gesture.spent) return;

    if (Math.abs(y - gesture.startY) > TAP_SLOP_PX) gesture.moved = true;

    const stepMs = now - gesture.lastAt;
    // Two samples in the same millisecond are not a speed. Leave the baseline
    // where it is so the next one measures across both of them.
    if (stepMs <= 0) return;
    const stepY = y - gesture.lastY;
    gesture.lastY = y;
    gesture.lastAt = now;

    if (stepY <= 0) return; // upward, or standing still
    if (y - gesture.startY < MIN_DRAG_PX) return;
    if (stepY / stepMs < FLICK_PX_PER_MS) return;

    gesture.spent = true;
    gesture.field.blur();
  };

  const onUp = () => {
    const ended = gesture;
    gesture = null;
    // A gesture that moved was a scroll, and it has already had its say.
    if (!ended || ended.moved || ended.spent) return;
    if (document.activeElement === ended.field) ended.field.blur();
  };

  const onCancel = () => {
    gesture = null;
  };

  // Capture, so a handler that stops propagation on its own control cannot
  // leave the keyboard stranded over the app.
  const opts = { capture: true, passive: true } as const;
  window.addEventListener('pointerdown', onDown, opts);
  window.addEventListener('pointermove', onMove, opts);
  window.addEventListener('touchmove', onMove, opts);
  window.addEventListener('pointerup', onUp, opts);
  window.addEventListener('pointercancel', onCancel, opts);

  return () => {
    window.removeEventListener('pointerdown', onDown, opts);
    window.removeEventListener('pointermove', onMove, opts);
    window.removeEventListener('touchmove', onMove, opts);
    window.removeEventListener('pointerup', onUp, opts);
    window.removeEventListener('pointercancel', onCancel, opts);
  };
}
