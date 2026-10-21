import { afterEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { VIEWPORT_LOCKED } from './zoomPin';

/**
 * The server-rendered sign-in pages' pre-paint script locks the viewport in the
 * iOS app (ISSUE-821). nginx's starting page carries the same script, held
 * equal by tests/test_nginx_starting_page.py, so running that file's copy runs
 * the login page's.
 */

const PLAIN_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148';
const SHELL = `${PLAIN_SAFARI} IstotaApp/0.2.0`;

const html = readFileSync(resolve(process.cwd(), '../docker/nginx/starting.html'), 'utf8');
const prePaint = /<script>(try\{[\s\S]*?)<\/script>/.exec(html)?.[1];
const initial = /<meta name="viewport" content="([^"]+)">/.exec(html)?.[1];

let meta: HTMLMetaElement | null = null;

afterEach(() => {
  meta?.remove();
  meta = null;
  document.documentElement.removeAttribute('data-theme');
});

function runPrePaint(ua: string): string {
  Object.defineProperty(window.navigator, 'userAgent', { value: ua, configurable: true });
  meta = document.createElement('meta');
  meta.name = 'viewport';
  meta.content = initial!;
  document.head.appendChild(meta);
  new Function(prePaint!)();
  return meta.content;
}

describe('sign-in page pre-paint viewport lock', () => {
  it('finds the script and the viewport tag', () => {
    expect(prePaint).toBeTruthy();
    expect(initial).toBe('width=device-width, initial-scale=1');
  });

  it('locks the viewport in the app', () => {
    expect(runPrePaint(SHELL)).toBe(VIEWPORT_LOCKED);
  });

  it('leaves the viewport alone in a browser', () => {
    expect(runPrePaint(PLAIN_SAFARI)).toBe(initial);
  });
});
