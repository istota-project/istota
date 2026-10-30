import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  HTML_FRAME_CSP,
  HTML_FRAME_SANDBOX,
  MAX_ASSETS,
  MAX_ASSET_BYTES,
  frameDocument,
  resolveRelative,
  MAX_TOTAL_BYTES,
  framePage,
} from './html';

const META = `<meta http-equiv="Content-Security-Policy" content="${HTML_FRAME_CSP}">`;
const parse = (framed: string) => new DOMParser().parseFromString(framed, 'text/html');

describe('framePage', () => {
  it('keeps a doctype, so the page keeps standards mode', () => {
    const framed = framePage(
      '<!DOCTYPE html>\n<html><head><title>T</title></head><body>Hi</body></html>',
    );
    expect(framed.startsWith(`<!DOCTYPE html><html><head>${META}`)).toBe(true);
    expect(parse(framed).title).toBe('T');
  });

  it('adds no doctype to a page without one', () => {
    expect(framePage('<p>Hi</p>').startsWith(`<html><head>${META}`)).toBe(true);
  });

  // JS `\s` matches these and HTML does not: text ahead of the doctype opens
  // the body, and a policy placed after it would be ignored.
  it.each(['\u00A0', '\u000B', '\uFEFF\uFEFF', '\u2003', '\uFEFF', ' \n\t', 'x', '<!-- c -->'])(
    'puts the policy first in the head whatever precedes the doctype: %j',
    (prefix) => {
      const framed = framePage(`${prefix}<!doctype html><img src="https://example.com/x.png">`);
      expect(framed).toMatch(
        /^(<!DOCTYPE html>)?<html><head><meta http-equiv="Content-Security-Policy"/,
      );
      const first = parse(framed).head.firstElementChild!;
      expect(first.getAttribute('http-equiv')).toBe('Content-Security-Policy');
      expect(first.getAttribute('content')).toBe(HTML_FRAME_CSP);
    },
  );

  it('opens links in a new tab with no opener and no referrer', () => {
    const doc = parse(
      framePage(
        '<meta name="referrer" content="unsafe-url"><a href="https://example.com" rel="opener" referrerpolicy="unsafe-url">a</a>' +
          '<map><area href="https://example.com" rel="opener"></map><a rel="opener">no href</a>' +
          '<svg><a href="https://example.com">svg</a></svg><img src="data:," referrerpolicy="unsafe-url">',
      ),
    );
    expect(doc.querySelector('base')!.getAttribute('target')).toBe('_blank');
    for (const link of doc.querySelectorAll('a, area')) {
      expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    }
    expect(doc.querySelectorAll('a, area')).toHaveLength(4);
    expect(doc.querySelector('[referrerpolicy]')).toBeNull();
    const referrers = [...doc.querySelectorAll('meta[name="referrer" i]')];
    expect(referrers.map((meta) => meta.getAttribute('content'))).toEqual(['no-referrer']);
  });

  it('keeps fragment links in the page and resolves the rest against the frame', () => {
    const doc = parse(
      framePage(
        '<a href="#intro" target="_blank">in</a><a href=" #x">sp</a><area href="#map">' +
          '<a href="//example.com/a">proto</a><a href="https://example.com/b">abs</a>' +
          '<h2 id="intro">Intro</h2>',
      ),
    );
    expect(doc.querySelector('base')!.getAttribute('href')).toBe('about:srcdoc');
    const links = [...doc.querySelectorAll('a, area')];
    expect(links.map((link) => link.getAttribute('target'))).toEqual([
      '_self',
      '_self',
      '_self',
      null,
      null,
    ]);
    expect(links[3].getAttribute('href')).toBe('https://example.com/a');
    expect(links[4].getAttribute('href')).toBe('https://example.com/b');
  });

  it('drops resource hints, which CSP does not govern', () => {
    const hints = [
      'dns-prefetch',
      'preconnect',
      'prefetch',
      'preload',
      'prerender',
      'modulepreload',
    ];
    const doc = parse(
      framePage(
        hints
          .map((rel) => `<link rel="${rel.toUpperCase()}" href="//receipt.example.com">`)
          .join('') + '<link rel="icon" href="data:,">',
      ),
    );
    expect([...doc.querySelectorAll('link')].map((link) => link.getAttribute('rel'))).toEqual([
      'icon',
    ]);
    expect(
      doc.querySelector('meta[http-equiv="x-dns-prefetch-control" i]')!.getAttribute('content'),
    ).toBe('off');
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

  it('frames a page with nothing relative exactly as framePage does, fetching nothing', async () => {
    const text = '<!doctype html><p>Hi <img src="https://example.com/x.png"></p>';
    expect(await frameDocument(text, '/docs/r.html', urlFor)).toBe(framePage(text));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('bounds the inlined copies, not only the fetched files', async () => {
    // One file in every fetch cap, named many times, must not multiply into the srcdoc.
    const bytes = new Uint8Array(MAX_ASSET_BYTES - 1);
    fetchMock.mockImplementation(async () => respond(bytes, 'image/png'));
    const framed = await frameDocument('<img src="a.png">'.repeat(10), '/docs/r.html', urlFor);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const srcs = [...parse(framed).querySelectorAll('img')].map((img) => img.getAttribute('src'));
    const inlined = srcs.filter((src) => src!.startsWith('data:'));
    expect(inlined.length).toBeGreaterThan(0);
    expect(inlined.reduce((sum, src) => sum + src!.length, 0)).toBeLessThanOrEqual(MAX_TOTAL_BYTES);
    expect(srcs.filter((src) => src === 'a.png')).toHaveLength(10 - inlined.length);
  });

  it('puts the policy first in the head on the inlining path too', async () => {
    const framed = await frameDocument(
      '\u00A0<!doctype html><img src="a.png">',
      '/docs/r.html',
      urlFor,
    );
    expect(parse(framed).head.firstElementChild!.getAttribute('http-equiv')).toBe(
      'Content-Security-Policy',
    );
  });

  it('inlines a relative raster through the viewer endpoint and drops its srcset', async () => {
    const text = '<!doctype html><p><img src="a.png" srcset="a@2x.png 2x"></p>';
    const framed = await frameDocument(text, '/docs/r.html', urlFor);
    expect(fetchMock).toHaveBeenCalledWith(urlFor('/docs/a.png'), expect.anything());
    const doc = new DOMParser().parseFromString(framed, 'text/html');
    const img = doc.querySelector('img')!;
    expect(img.getAttribute('src')).toBe('data:image/png;base64,iVBORw==');
    expect(img.hasAttribute('srcset')).toBe(false);
    expect(framed.startsWith(`<!DOCTYPE html><html><head>${META}`)).toBe(true);
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
