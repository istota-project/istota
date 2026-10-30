import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  HTML_FRAME_CSP,
  HTML_FRAME_SANDBOX,
  MAX_ASSETS,
  MAX_ASSET_BYTES,
  frameDocument,
  resolveRelative,
  sandboxedDocument,
} from './html';

const META = `<meta http-equiv="Content-Security-Policy" content="${HTML_FRAME_CSP}">`;

describe('sandboxedDocument', () => {
  it('puts the policy after a leading doctype, so the page keeps standards mode', () => {
    const page = '﻿\n<!DOCTYPE html>\n<html><head><title>T</title></head><body>Hi</body></html>';
    const framed = sandboxedDocument(page);
    expect(framed.startsWith('﻿\n<!DOCTYPE html>' + META)).toBe(true);
    expect(framed.endsWith('<html><head><title>T</title></head><body>Hi</body></html>')).toBe(true);
  });

  it('puts the policy first in a fragment with no doctype', () => {
    expect(sandboxedDocument('<p>Hi</p>').startsWith(META)).toBe(true);
  });

  it('opens links in a new tab, ahead of anything the page sets', () => {
    expect(sandboxedDocument('<p>Hi</p>')).toContain(`${META}<base target="_blank">`);
  });

  it('allows no script, no remote load and no form', () => {
    expect(HTML_FRAME_SANDBOX.split(' ').sort()).toEqual([
      'allow-popups',
      'allow-popups-to-escape-sandbox',
    ]);
    expect(HTML_FRAME_CSP).toBe(
      "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:",
    );
  });
});

describe('resolveRelative', () => {
  it.each([
    ['/docs/r.html', 'img/a.png', '/docs/img/a.png'],
    ['/docs/r.html', './img/a.png', '/docs/img/a.png'],
    ['/docs/r.html', '../a.png', '/a.png'],
    ['/docs/r.html', 'a%20b.png', '/docs/a b.png'],
    ['/docs/r.html', 'a.png?v=1#top', '/docs/a.png'],
    ['r.html', 'a.png', 'a.png'],
    ['sub/r.html', '../a.png', 'a.png'],
  ])('resolves %s + %s to %s', (doc, ref, expected) => {
    expect(resolveRelative(doc, ref)).toBe(expected);
  });

  it.each([
    'https://example.com/a.png',
    '//example.com/a.png',
    '/abs.png',
    'data:image/png;base64,AAAA',
    'javascript:alert(1)',
    '../../a.png',
    '#top',
    '',
    'a%zz.png',
    'dir/',
  ])('leaves %j alone', (ref) => {
    expect(resolveRelative('/docs/r.html', ref)).toBeNull();
  });
});

describe('frameDocument', () => {
  const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
  const urlFor = (path: string) => `/files?path=${encodeURIComponent(path)}`;
  let fetchMock: ReturnType<typeof vi.fn>;

  function respond(body: BodyInit, type: string, extra: Record<string, string> = {}) {
    return new Response(body, { status: 200, headers: { 'Content-Type': type, ...extra } });
  }

  beforeEach(() => {
    fetchMock = vi.fn(async (url: string) => {
      const path = new URL(url, 'http://app').searchParams.get('path');
      if (path === '/docs/a.png') return respond(PNG, 'image/png');
      if (path === '/docs/style.css') return respond('p { color: red }', 'text/css');
      if (path === '/docs/page.svg') return respond('<svg/>', 'image/svg+xml');
      if (path === '/docs/huge.png')
        return respond(PNG, 'image/png', { 'Content-Length': String(MAX_ASSET_BYTES + 1) });
      return new Response('', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('frames a page with nothing relative exactly as sandboxedDocument does, fetching nothing', async () => {
    const text = '<!doctype html><p>Hi <img src="https://example.com/x.png"></p>';
    expect(await frameDocument(text, '/docs/r.html', urlFor)).toBe(sandboxedDocument(text));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('inlines a relative raster through the viewer endpoint and drops its srcset', async () => {
    const text = '<!doctype html><p><img src="a.png" srcset="a@2x.png 2x"></p>';
    const framed = await frameDocument(text, '/docs/r.html', urlFor);
    expect(fetchMock).toHaveBeenCalledWith(urlFor('/docs/a.png'), expect.anything());
    const doc = new DOMParser().parseFromString(framed, 'text/html');
    const img = doc.querySelector('img')!;
    expect(img.getAttribute('src')).toBe('data:image/png;base64,iVBORw==');
    expect(img.hasAttribute('srcset')).toBe(false);
    expect(framed.startsWith('<!DOCTYPE html>' + META)).toBe(true);
  });

  it('inlines a relative stylesheet as a style element', async () => {
    const text = '<html><head><link rel="Stylesheet" href="style.css"></head><p>Hi</p></html>';
    const doc = new DOMParser().parseFromString(
      await frameDocument(text, '/docs/r.html', urlFor),
      'text/html',
    );
    expect(doc.querySelector('link')).toBeNull();
    expect(doc.querySelector('style')!.textContent).toBe('p { color: red }');
  });

  it('leaves an image alone that is not a raster, too large, or refused', async () => {
    const text = '<img src="page.svg"><img src="huge.png"><img src="missing.png">';
    const doc = new DOMParser().parseFromString(
      await frameDocument(text, '/docs/r.html', urlFor),
      'text/html',
    );
    expect([...doc.querySelectorAll('img')].map((img) => img.getAttribute('src'))).toEqual([
      'page.svg',
      'huge.png',
      'missing.png',
    ]);
  });

  it(`fetches at most ${MAX_ASSETS} distinct files`, async () => {
    const imgs = Array.from({ length: MAX_ASSETS + 5 }, (_, i) => `<img src="i${i}.png">`);
    await frameDocument(imgs.join('') + '<img src="i0.png">', '/docs/r.html', urlFor);
    expect(fetchMock).toHaveBeenCalledTimes(MAX_ASSETS);
  });
});
