import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  VIEWPORT_DEFAULT,
  VIEWPORT_LOCKED,
  VIEWPORT_NO_ZOOM,
  pinViewportZoom,
  releaseViewportZoom,
} from './zoomPin';

/**
 * In the iOS app the page never zooms (ISSUE-818); in a browser pinch-zoom
 * stays, and only a focused sub-16px field pins it (ISSUE-219).
 */

const PLAIN_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148';
const SHELL = `${PLAIN_SAFARI} IstotaApp/0.2.0`;

function setUserAgent(ua: string): void {
  Object.defineProperty(window.navigator, 'userAgent', { value: ua, configurable: true });
}

let meta: HTMLMetaElement;

beforeEach(() => {
  meta = document.createElement('meta');
  meta.name = 'viewport';
  meta.content = VIEWPORT_DEFAULT;
  document.head.append(meta);
});

afterEach(() => {
  meta.remove();
  setUserAgent(PLAIN_SAFARI);
});

describe('zoomPin', () => {
  it('pins on focus and restores pinch-zoom on blur in a browser', () => {
    setUserAgent(PLAIN_SAFARI);
    pinViewportZoom();
    expect(meta.content).toBe(VIEWPORT_NO_ZOOM);
    releaseViewportZoom();
    expect(meta.content).toBe(VIEWPORT_DEFAULT);
  });

  it('keeps the shell locked through a pin and a release', () => {
    setUserAgent(SHELL);
    meta.content = VIEWPORT_LOCKED;
    pinViewportZoom();
    expect(meta.content).toBe(VIEWPORT_LOCKED);
    releaseViewportZoom();
    expect(meta.content).toBe(VIEWPORT_LOCKED);
  });

  it('keeps viewport-fit=cover in every string it writes', () => {
    for (const content of [VIEWPORT_DEFAULT, VIEWPORT_NO_ZOOM, VIEWPORT_LOCKED]) {
      expect(content).toContain('viewport-fit=cover');
    }
    expect(VIEWPORT_LOCKED).toContain('maximum-scale=1');
    expect(VIEWPORT_LOCKED).toContain('user-scalable=no');
  });
});

describe('app.html pre-paint viewport lock', () => {
  const html = readFileSync(resolve(process.cwd(), 'src/app.html'), 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);

  /** Run app.html's inline scripts against a document carrying its viewport meta. */
  function runPrePaint(ua: string): string {
    setUserAgent(ua);
    const initial = /<meta name="viewport" content="([^"]+)"/.exec(html)?.[1];
    expect(initial).toBe(VIEWPORT_DEFAULT);
    meta.content = initial!;
    for (const source of scripts) new Function(source)();
    return meta.content;
  }

  it('locks the viewport before first paint in the app', () => {
    // After hydration would leave a window in which the page can still zoom.
    expect(runPrePaint(SHELL)).toBe(VIEWPORT_LOCKED);
  });

  it('leaves the viewport alone in a browser', () => {
    expect(runPrePaint(PLAIN_SAFARI)).toBe(VIEWPORT_DEFAULT);
  });
});
