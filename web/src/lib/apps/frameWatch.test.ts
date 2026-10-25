import { describe, expect, it, vi } from 'vitest';
import { watchFrame } from './frameWatch';

function load(frame: HTMLIFrameElement) {
  frame.dispatchEvent(new Event('load'));
}

describe('watchFrame', () => {
  it('allows the first load: the installed document', () => {
    const frame = document.createElement('iframe');
    const onNavigated = vi.fn();
    const watch = watchFrame(frame, onNavigated);
    load(frame);
    expect(onNavigated).not.toHaveBeenCalled();
    expect(watch.navigations).toBe(0);
    expect(watch.navigated).toBe(false);
  });

  it('calls the handler once on the second load, and counts every later one', () => {
    const frame = document.createElement('iframe');
    const onNavigated = vi.fn();
    const watch = watchFrame(frame, onNavigated);
    load(frame);
    load(frame);
    expect(onNavigated).toHaveBeenCalledTimes(1);
    expect(watch.navigated).toBe(true);
    load(frame);
    expect(onNavigated).toHaveBeenCalledTimes(1);
    expect(watch.navigations).toBe(2);
  });

  it('stops counting once disposed', () => {
    const frame = document.createElement('iframe');
    const onNavigated = vi.fn();
    const watch = watchFrame(frame, onNavigated);
    watch.dispose();
    load(frame);
    load(frame);
    expect(onNavigated).not.toHaveBeenCalled();
  });
});
