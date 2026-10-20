import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { debounce } from './debounce';
import { latest } from './latest';
import { isEditableTarget } from './editableTarget';
import { onDocumentVisible } from './visibility';
import { startPolling } from './polling.svelte';

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  setVisibility('visible');
});

describe('debounce', () => {
  it('calls once with the last arguments after the quiet period', () => {
    const fn = vi.fn();
    const d = debounce(fn, 300);
    d('a');
    vi.advanceTimersByTime(200);
    d('b');
    vi.advanceTimersByTime(299);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledExactlyOnceWith('b');
  });

  it('drops a pending call on cancel', () => {
    const fn = vi.fn();
    const d = debounce(fn, 300);
    d();
    d.cancel();
    vi.advanceTimersByTime(1000);
    expect(fn).not.toHaveBeenCalled();
  });
});

describe('latest', () => {
  it('reports only the newest request as current', () => {
    const loads = latest();
    const first = loads.next();
    expect(first()).toBe(true);
    const second = loads.next();
    expect(first()).toBe(false);
    expect(second()).toBe(true);
  });

  it('turns every check false once disposed, including later ones', () => {
    const loads = latest();
    const current = loads.next();
    loads.dispose();
    expect(current()).toBe(false);
    expect(loads.next()()).toBe(false);
  });
});

describe('isEditableTarget', () => {
  it.each([
    ['<input />', 'input'],
    ['<textarea></textarea>', 'textarea'],
    ['<select></select>', 'select'],
    ['<div contenteditable="true"><b>x</b></div>', 'b'],
  ])('is true inside %s', (html, selector) => {
    document.body.innerHTML = html;
    const el = document.querySelector(selector)!;
    expect(isEditableTarget(el)).toBe(true);
    expect(isEditableTarget(new KeyboardEvent('keydown'))).toBe(false);
    const event = new KeyboardEvent('keydown', { bubbles: true });
    let seen = false;
    el.addEventListener('keydown', (e) => (seen = isEditableTarget(e)));
    el.dispatchEvent(event);
    expect(seen).toBe(true);
  });

  it('is false for a button, a contenteditable="false" region and no target', () => {
    document.body.innerHTML = '<button>x</button><div contenteditable="false"><i>y</i></div>';
    expect(isEditableTarget(document.querySelector('button'))).toBe(false);
    expect(isEditableTarget(document.querySelector('i'))).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
  });
});

describe('onDocumentVisible', () => {
  it('runs on a return to visible, not on hiding, until cleaned up', () => {
    const fn = vi.fn();
    const off = onDocumentVisible(fn);
    setVisibility('hidden');
    expect(fn).not.toHaveBeenCalled();
    setVisibility('visible');
    expect(fn).toHaveBeenCalledTimes(1);
    off();
    setVisibility('hidden');
    setVisibility('visible');
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('startPolling', () => {
  it('calls on the interval and stops when told', () => {
    const fn = vi.fn();
    const stop = startPolling(fn, 1000);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(3000);
    expect(fn).toHaveBeenCalledTimes(3);
    stop();
    vi.advanceTimersByTime(3000);
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it('pauses while hidden and refreshes once on return', () => {
    const fn = vi.fn();
    const stop = startPolling(fn, 1000);
    setVisibility('hidden');
    vi.advanceTimersByTime(5000);
    expect(fn).not.toHaveBeenCalled();
    setVisibility('visible');
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1000);
    expect(fn).toHaveBeenCalledTimes(2);
    stop();
  });

  it('keeps going while hidden when asked to', () => {
    const fn = vi.fn();
    const stop = startPolling(fn, 1000, { pauseWhenHidden: false });
    setVisibility('hidden');
    vi.advanceTimersByTime(2000);
    expect(fn).toHaveBeenCalledTimes(2);
    stop();
  });
});
