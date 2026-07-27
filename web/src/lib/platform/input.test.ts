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

function pointerDown(el: Element): void {
  el.dispatchEvent(new Event('pointerdown', { bubbles: true }));
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

    pointerDown(pane);

    expect(document.activeElement).not.toBe(field);
  });

  it('dismisses on the first tap, not the second', () => {
    // The platform behaviour this replaces: iOS keeps the keyboard up through
    // a tap on non-focusable content, so it took two.
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();
    const blur = vi.spyOn(field, 'blur');

    pointerDown(pane);

    expect(blur).toHaveBeenCalledTimes(1);
  });

  it('leaves the keyboard up for the composer own controls', () => {
    // Tapping send must not dismiss on the way to submitting — the composer
    // decides that for itself, after the message has gone.
    const { field, send } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(send);

    expect(document.activeElement).toBe(field);
  });

  it('leaves the keyboard up for a tap on the field itself', () => {
    const { field } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(field);

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

    pointerDown(other);

    expect(document.activeElement).toBe(field);
  });

  it('dismisses when a scroll gesture starts over the transcript', () => {
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pane.dispatchEvent(new Event('touchmove', { bubbles: true }));

    expect(document.activeElement).not.toBe(field);
  });

  it('does nothing when no text entry is focused', () => {
    const { pane } = layout();
    stop = installKeyboardDismiss();
    const blur = vi.spyOn(HTMLElement.prototype, 'blur');

    pointerDown(pane);

    expect(blur).not.toHaveBeenCalled();
  });

  it('stays out of the way on a device with a hardware keyboard', () => {
    setPointer('fine');
    const { field, pane } = layout();
    stop = installKeyboardDismiss();
    field.focus();

    pointerDown(pane);

    expect(document.activeElement).toBe(field);
  });

  it('stops listening on teardown', () => {
    const { field, pane } = layout();
    const release = installKeyboardDismiss();
    field.focus();
    release();

    pointerDown(pane);

    expect(document.activeElement).toBe(field);
  });
});
