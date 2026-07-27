import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { installViewportGuard } from './viewport';

/**
 * The guard exists to survive soft-keyboard transitions, so the cases worth
 * pinning are the ones where the platform lies: focus retained after the
 * keyboard is dismissed, a viewport that keeps reporting itself short, and a
 * reading that is still moving when the first sample is taken.
 */

type VV = {
  height: number;
  scale: number;
  addEventListener: (t: string, f: () => void) => void;
  removeEventListener: (t: string, f: () => void) => void;
};

let insets: Record<string, string>;
let teardown: (() => void) | undefined;

function setVisualViewport(height: number | null): VV | null {
  if (height == null) {
    Object.defineProperty(window, 'visualViewport', { value: null, configurable: true });
    return null;
  }
  const vv: VV = {
    height,
    scale: 1,
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
  return vv;
}

function standalone(on: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: on && q.includes('standalone'),
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}

function appHeight(): string {
  return document.documentElement.style.getPropertyValue('--app-height');
}

beforeEach(() => {
  vi.useFakeTimers();
  insets = { top: '47px', bottom: '34px', left: '0px', right: '0px' };
  // The probe resolves env() through getComputedStyle; jsdom has no env(), so
  // the padding values are served from `insets` and mutated per scenario.
  vi.spyOn(window, 'getComputedStyle').mockImplementation(
    (el: Element) =>
      ({
        getPropertyValue: (p: string) =>
          (el as HTMLElement).getAttribute('aria-hidden') === 'true'
            ? (insets[p.replace('padding-', '')] ?? '')
            : '',
      }) as unknown as CSSStyleDeclaration,
  );
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
  Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true, writable: true });
  standalone(true);
  setVisualViewport(800);
  document.body.innerHTML = '';
});

afterEach(() => {
  teardown?.();
  teardown = undefined;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function focusTextEntry(): HTMLTextAreaElement {
  const ta = document.createElement('textarea');
  document.body.appendChild(ta);
  ta.focus();
  return ta;
}

describe('installViewportGuard', () => {
  it('publishes the measured insets and, in standalone, the app height', () => {
    teardown = installViewportGuard();
    const root = document.documentElement.style;
    expect(root.getPropertyValue('--safe-top')).toBe('47px');
    expect(root.getPropertyValue('--safe-bottom')).toBe('34px');
    expect(appHeight()).toBe('800px');
  });

  it('leaves --app-height alone outside standalone', () => {
    standalone(false);
    teardown = installViewportGuard();
    expect(appHeight()).toBe('');
  });

  it('holds its reading while the keyboard is up', () => {
    teardown = installViewportGuard();
    vi.advanceTimersByTime(2000); // establish the keyboard-free baseline
    focusTextEntry();
    setVisualViewport(400); // keyboard eating half the visual viewport
    insets = { top: '0px', bottom: '0px', left: '0px', right: '0px' };
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(2000);
    // The collapsed insets iOS reports mid-keyboard must not be latched.
    expect(document.documentElement.style.getPropertyValue('--safe-bottom')).toBe('34px');
  });

  it('holds its reading when the keyboard shrinks the layout viewport instead', () => {
    // An installed iOS app resizes the *layout* viewport for the keyboard, so
    // both heights drop together and nothing looks occluded. Reading occlusion
    // alone called this "no keyboard" and latched the keyboard-sized height —
    // a permanent band at the bottom, every time the keyboard opened.
    teardown = installViewportGuard();
    vi.advanceTimersByTime(2000);
    expect(appHeight()).toBe('800px');

    focusTextEntry();
    Object.defineProperty(window, 'innerHeight', { value: 420, configurable: true });
    setVisualViewport(420);
    insets = { top: '0px', bottom: '0px', left: '0px', right: '0px' };
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(2000);

    expect(appHeight()).toBe('800px');
    expect(document.documentElement.style.getPropertyValue('--safe-bottom')).toBe('34px');
  });

  it('re-measures when the keyboard is dismissed with focus retained', () => {
    teardown = installViewportGuard();
    vi.advanceTimersByTime(2000); // baseline: 800 / 800
    const ta = focusTextEntry();
    setVisualViewport(400);
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(500);

    // Swipe-to-dismiss: both viewports come back to the keyboard-free geometry
    // but iOS keeps focus on the field. A focus-only test treats this as
    // keyboard-up forever and never measures again.
    setVisualViewport(800);
    insets = { ...insets, bottom: '20px' };
    expect(document.activeElement).toBe(ta);
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(2000);

    expect(document.documentElement.style.getPropertyValue('--safe-bottom')).toBe('20px');
  });

  it('does not treat a half-restored viewport as a dismissal', () => {
    teardown = installViewportGuard();
    vi.advanceTimersByTime(2000);
    focusTextEntry();
    // Layout viewport back, visual viewport still short: the keyboard is up.
    setVisualViewport(400);
    insets = { top: '0px', bottom: '0px', left: '0px', right: '0px' };
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(2000);
    expect(document.documentElement.style.getPropertyValue('--safe-bottom')).toBe('34px');
  });

  it('never publishes a keyboard-shaped height after a dismissal', () => {
    // Tapping outside the field to dismiss: the layout viewport comes back
    // slowly, or not until some later event. Holding the last whole height
    // leaves the app a touch too tall for a moment; publishing the short one
    // leaves a band at the bottom that nothing afterwards corrects.
    teardown = installViewportGuard();
    vi.advanceTimersByTime(2000);
    const ta = focusTextEntry();
    Object.defineProperty(window, 'innerHeight', { value: 420, configurable: true });
    setVisualViewport(420);
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(400);

    ta.blur();
    window.dispatchEvent(new Event('focusout'));
    vi.advanceTimersByTime(5000);

    expect(appHeight()).toBe('800px');
  });

  it('pins the height to the tallest the viewport has been', () => {
    // Back to within the tolerance but not all the way is still a band, just a
    // smaller one — and the height it ought to be is already known.
    teardown = installViewportGuard();
    vi.advanceTimersByTime(2000);
    Object.defineProperty(window, 'innerHeight', { value: 780, configurable: true });
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(2000);
    expect(appHeight()).toBe('800px');
  });

  it('adopts a smaller window when no keyboard was involved', () => {
    // Same short reading, no dismissal behind it (split view, a resized
    // window): once it has held for the whole window it is simply the truth.
    teardown = installViewportGuard();
    vi.advanceTimersByTime(2000);
    Object.defineProperty(window, 'innerHeight', { value: 600, configurable: true });
    setVisualViewport(600);
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(5000);

    expect(appHeight()).toBe('600px');
  });

  it('publishes the app height while only the visual viewport is short', () => {
    // The iOS 26 residue: visualViewport stays short indefinitely after the
    // keyboard has gone. The layout viewport is whole, which is the only thing
    // the app height depends on.
    teardown = installViewportGuard();
    vi.advanceTimersByTime(2000);
    setVisualViewport(400);
    Object.defineProperty(window, 'innerHeight', { value: 900, configurable: true });
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(2000);

    expect(appHeight()).toBe('900px');
  });

  it('keeps sampling until the reading stops moving', () => {
    teardown = installViewportGuard();
    // A transition that is still in motion when the first sample lands: a
    // single fixed-delay re-measure would latch whichever frame it hit.
    const heights = [500, 640, 900, 900, 900];
    let i = 0;
    Object.defineProperty(window, 'innerHeight', {
      configurable: true,
      get: () => heights[Math.min(i++, heights.length - 1)],
    });
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(2000);
    expect(appHeight()).toBe('900px');
  });

  it('unwinds the residual scroll the keyboard leaves behind', () => {
    // What only happens once text has been typed: iOS scrolls the layout
    // viewport to track the caret and does not always put it back.
    Object.defineProperty(document.documentElement, 'scrollTop', {
      value: 120,
      configurable: true,
      writable: true,
    });
    Object.defineProperty(document.documentElement, 'scrollHeight', {
      value: 800,
      configurable: true,
    });
    Object.defineProperty(document.documentElement, 'clientHeight', {
      value: 800,
      configurable: true,
    });
    teardown = installViewportGuard();
    expect(window.scrollTo).toHaveBeenCalledWith(0, 0);
  });

  it('leaves a genuinely scrollable document scrolled where the user left it', () => {
    Object.defineProperty(document.documentElement, 'scrollTop', {
      value: 120,
      configurable: true,
      writable: true,
    });
    Object.defineProperty(document.documentElement, 'scrollHeight', {
      value: 4000,
      configurable: true,
    });
    Object.defineProperty(document.documentElement, 'clientHeight', {
      value: 800,
      configurable: true,
    });
    teardown = installViewportGuard();
    expect(window.scrollTo).not.toHaveBeenCalled();
  });

  it('falls back to focus alone when there is no visualViewport', () => {
    setVisualViewport(null);
    teardown = installViewportGuard();
    focusTextEntry();
    insets = { ...insets, bottom: '0px' };
    window.dispatchEvent(new Event('resize'));
    vi.advanceTimersByTime(2000);
    expect(document.documentElement.style.getPropertyValue('--safe-bottom')).toBe('34px');
  });

  it('removes what it published on teardown', () => {
    const stop = installViewportGuard();
    stop();
    expect(appHeight()).toBe('');
    expect(document.documentElement.style.getPropertyValue('--safe-top')).toBe('');
    expect(document.querySelector('[aria-hidden="true"]')).toBeNull();
  });
});
