import { describe, it, expect, afterEach, vi } from 'vitest';
import { isNativeShell, onKeyboardGeometry, shellAtLeast, shellVersion } from './native';

function setUserAgent(ua: string): void {
  Object.defineProperty(window.navigator, 'userAgent', { value: ua, configurable: true });
}

const PLAIN_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 ' +
  '(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';

afterEach(() => {
  setUserAgent(PLAIN_SAFARI);
});

describe('shellVersion', () => {
  it('reads the version the shell appends to the User-Agent', () => {
    setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.2.0`);
    expect(shellVersion()).toBe('0.2.0');
  });

  it('is null in an ordinary browser', () => {
    setUserAgent(PLAIN_SAFARI);
    expect(shellVersion()).toBeNull();
    expect(isNativeShell()).toBe(false);
  });

  it('does not match a lookalike token', () => {
    setUserAgent(`${PLAIN_SAFARI} NotIstotaApp/9.9.9`);
    expect(shellVersion()).toBeNull();
  });
});

describe('shellAtLeast', () => {
  it('compares component-wise, not lexically', () => {
    setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.10.0`);
    // Lexically "0.10.0" < "0.9.0"; numerically it is not.
    expect(shellAtLeast('0.9.0')).toBe(true);
  });

  it('accepts the exact version', () => {
    setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.2.0`);
    expect(shellAtLeast('0.2.0')).toBe(true);
  });

  it('rejects an older shell', () => {
    setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.1.0`);
    expect(shellAtLeast('0.2.0')).toBe(false);
  });

  it('rejects a browser outright', () => {
    setUserAgent(PLAIN_SAFARI);
    expect(shellAtLeast('0.0.1')).toBe(false);
  });

  it('ignores a prerelease suffix rather than failing the parse', () => {
    setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.2.0-beta.1`);
    expect(shellAtLeast('0.2.0')).toBe(true);
  });
});

describe('onKeyboardGeometry', () => {
  it('reports the height the shell sends with keyboardWillShow', () => {
    const seen: number[] = [];
    const stop = onKeyboardGeometry((h) => seen.push(h));

    window.dispatchEvent(new CustomEvent('keyboardWillShow', { detail: { keyboardHeight: 336 } }));
    expect(seen).toEqual([336]);

    stop();
  });

  it('reports zero on keyboardWillHide', () => {
    const seen: number[] = [];
    const stop = onKeyboardGeometry((h) => seen.push(h));

    window.dispatchEvent(new CustomEvent('keyboardWillShow', { detail: { keyboardHeight: 336 } }));
    window.dispatchEvent(new CustomEvent('keyboardWillHide'));
    expect(seen).toEqual([336, 0]);

    stop();
  });

  it('treats a malformed payload as no keyboard rather than throwing', () => {
    const handler = vi.fn();
    const stop = onKeyboardGeometry(handler);

    window.dispatchEvent(new CustomEvent('keyboardWillShow'));
    expect(handler).toHaveBeenCalledWith(0);

    stop();
  });

  it('stops listening once released', () => {
    const handler = vi.fn();
    const stop = onKeyboardGeometry(handler);
    stop();

    window.dispatchEvent(new CustomEvent('keyboardWillShow', { detail: { keyboardHeight: 336 } }));
    expect(handler).not.toHaveBeenCalled();
  });
});
