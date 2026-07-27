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

/**
 * Dismiss the soft keyboard on the first tap outside the composer, and when a
 * scroll gesture starts.
 *
 * iOS keeps the keyboard up through a tap on non-focusable content — a
 * paragraph in the transcript is not a focus target, so nothing takes focus off
 * the field and nothing dismisses. In practice that means tapping twice: once
 * for something to happen, once for the keyboard to go. This makes the first
 * tap enough.
 *
 * `pointerdown` rather than `click`, for two reasons. It is the start of the
 * gesture, so a tap dismisses without waiting for the finger to lift; and a
 * scroll begins with a pointerdown too, so dragging the transcript dismisses on
 * the same rule with nothing extra. `touchmove` is a backstop for a drag that
 * begins somewhere pointerdown was not delivered.
 *
 * Three things are deliberately exempt: the composer and anything marked
 * `data-keeps-keyboard` (a control that acts on the text it is next to), the
 * focused field itself, and any other text entry — moving between two fields is
 * one keyboard, and blurring in between bounces it down and straight back up.
 */
export function installKeyboardDismiss(): () => void {
  if (typeof window === 'undefined') return () => {};

  const onGesture = (e: Event) => {
    if (!usesSoftKeyboard()) return;
    const field = focusedTextEntry();
    if (!field) return;

    const target = e.target as Element | null;
    if (!target || typeof target.closest !== 'function') return;
    if (target === field || isTextEntry(target)) return;
    if (target.closest(KEEPS_KEYBOARD)) return;

    field.blur();
  };

  // Capture, so a handler that stops propagation on its own control cannot
  // leave the keyboard stranded over the app.
  const opts = { capture: true, passive: true } as const;
  window.addEventListener('pointerdown', onGesture, opts);
  window.addEventListener('touchmove', onGesture, opts);

  return () => {
    window.removeEventListener('pointerdown', onGesture, opts);
    window.removeEventListener('touchmove', onGesture, opts);
  };
}
