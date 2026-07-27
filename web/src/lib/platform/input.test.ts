import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { installKeyboardDismiss, usesSoftKeyboard } from './input';

let stop: (() => void) | undefined;

function setPointer(kind: 'coarse' | 'fine'): void {
  window.matchMedia = ((q: string) => ({
    matches: q.includes(kind),
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}

/** The composer, its send button, and an unrelated pane to tap in. */
function layout(): { field: HTMLTextAreaElement; send: HTMLElement; pane: HTMLElement } {
  document.body.innerHTML = `
    <main data-testid="pane"><p>a message</p></main>
    <div class="composer">
      <textarea data-testid="field"></textarea>
      <button data-testid="send">Send</button>
    </div>`;
  return {
    field: document.querySelector('[data-testid="field"]')!,
    send: document.querySelector('[data-testid="send"]')!,
    pane: document.querySelector('[data-testid="pane"]')!,
  };
}

/** A gesture is a down, some number of moves, and a lift. `y` is the only axis
 *  that matters — the dismiss is a vertical drag. */
function pointerDown(el: Element, y = 100): void {
  el.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, clientY: y }));
}

function pointerMove(el: Element, y: number): void {
  el.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientY: y }));
}

function pointerUp(el: Element, y = 100): void {
  el.dispatchEvent(new MouseEvent('pointerup', { bubbles: true, clientY: y }));
}

/** Down and straight back up, with nothing in between. */
function tap(el: Element, y = 100): void {
  pointerDown(el, y);
  pointerUp(el, y);
}

/** The touch backstop, which carries its coordinate in `touches` instead. */
function touchMove(el: Element, y: number): void {
  const e = new Event('touchmove', { bubbles: true });
  Object.defineProperty(e, 'touches', { value: [{ clientY: y }] });
  el.dispatchEvent(e);
}

beforeEach(() => {
  setPointer('coarse');
});

afterEach(() => {
  stop?.();
  stop = undefined;
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});

describe('usesSoftKeyboard', () => {
  it('is true on a coarse pointer and false on a fine one', () => {
    setPointer('coarse');
    expect(usesSoftKeyboard()).toBe(true);
    setPointer('fine');
    expect(usesSoftKeyboard()).toBe(false);
  });
});

describe('installKeyboardDismiss', () => {
  it('dismisses on a single tap outside the composer', () => {
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    tap(pane);

    expect(document.activeElement).not.toBe(field);
  });

  it('dismisses on the first tap, not the second', () => {
    // The platform behaviour this replaces: iOS keeps the keyboard up through
    // a tap on non-focusable content, so it took two.
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();
    const blur = vi.spyOn(field, 'blur');

    tap(pane);

    expect(blur).toHaveBeenCalledTimes(1);
  });

  it('leaves the keyboard up for the composer own controls', () => {
    // Tapping send must not dismiss on the way to submitting — the composer
    // decides that for itself, after the message has gone.
    const { field, send } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    tap(send);

    expect(document.activeElement).toBe(field);
  });

  it('leaves the keyboard up for a tap on the field itself', () => {
    const { field } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    tap(field);

    expect(document.activeElement).toBe(field);
  });

  it('does not steal focus from a tap on another text field', () => {
    // Moving between two fields is one keyboard, not two — blurring here would
    // bounce the keyboard down and straight back up.
    const { field } = layout();
    const other = document.createElement('input');
    document.body.appendChild(other);
    stop = installKeyboardDismiss();
    field.focus();

    tap(other);

    expect(document.activeElement).toBe(field);
  });

  it('does nothing when no text entry is focused', () => {
    const { pane } = layout();
    stop = installKeyboardDismiss();
    const blur = vi.spyOn(HTMLElement.prototype, 'blur');

    tap(pane);

    expect(blur).not.toHaveBeenCalled();
  });

  it('stays out of the way on a device with a hardware keyboard', () => {
    setPointer('fine');
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    tap(pane);

    expect(document.activeElement).toBe(field);
  });

  it('stops listening on teardown', () => {
    const { field, pane } = layout();
    const release = installKeyboardDismiss();
    field.focus();
    release();

    tap(pane);

    expect(document.activeElement).toBe(field);
  });
});

describe('installKeyboardDismiss scroll gating', () => {
  it('keeps the keyboard up through a small scroll', () => {
    // The whole point: nudging the transcript to re-read what you just typed is
    // not "I'm done typing", and it used to cost the keyboard every time.
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(pane, 100);
    pointerMove(pane, 112);
    pointerMove(pane, 124);

    expect(document.activeElement).toBe(field);
  });

  it('dismisses once a downward drag passes the threshold', () => {
    // The deliberate gesture — pulling the keyboard down with the content.
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(pane, 100);
    pointerMove(pane, 140);
    pointerMove(pane, 200);

    expect(document.activeElement).not.toBe(field);
  });

  it('keeps the keyboard up for a drag the other way', () => {
    // Dragging up runs the transcript toward the newest message, which is where
    // the keyboard already is. Only the downward pull dismisses.
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(pane, 300);
    pointerMove(pane, 200);
    pointerMove(pane, 100);

    expect(document.activeElement).toBe(field);
  });

  it('measures the drag from where it started, not the last move', () => {
    // A slow drag arrives as many small moves. Summing the gaps rather than
    // comparing against the origin would never reach the threshold.
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(pane, 100);
    for (let y = 105; y <= 180; y += 5) pointerMove(pane, y);

    expect(document.activeElement).not.toBe(field);
  });

  it('does not dismiss on the lift that ends a scroll', () => {
    // A scroll that stayed under the threshold has already been judged. The
    // finger coming off it is not a tap and must not dismiss on the way out.
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(pane, 100);
    pointerMove(pane, 130);
    pointerUp(pane, 130);

    expect(document.activeElement).toBe(field);
  });

  it('dismisses only once across a long drag', () => {
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();
    const blur = vi.spyOn(field, 'blur');

    pointerDown(pane, 100);
    pointerMove(pane, 200);
    pointerMove(pane, 300);
    pointerUp(pane, 300);

    expect(blur).toHaveBeenCalledTimes(1);
  });

  it('gates a touchmove drag the same way when no pointerdown arrived', () => {
    // The backstop for a drag that begins where pointerdown was not delivered.
    // The first move is the origin it has to measure from.
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    touchMove(pane, 100);
    touchMove(pane, 118);
    expect(document.activeElement).toBe(field);

    touchMove(pane, 220);
    expect(document.activeElement).not.toBe(field);
  });

  it('forgets a gesture that was cancelled', () => {
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(pane, 100);
    pane.dispatchEvent(new MouseEvent('pointercancel', { bubbles: true, clientY: 100 }));
    pointerUp(pane, 100);

    expect(document.activeElement).toBe(field);
  });
});
