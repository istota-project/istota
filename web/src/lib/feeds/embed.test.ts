/**
 * Player URLs for Are.na Embed blocks.
 *
 * The backend stores only the canonical watch URL (`embed_url`), never the
 * provider's own `<iframe>` — keeping a third-party frame out of feed HTML is
 * what lets the sanitizer stay tight for every RSS feed. So the reader has to
 * rebuild the player, and this is the whole trust boundary: whatever comes out
 * of `playerUrl` goes straight into an iframe `src`. Anything not positively
 * recognised as a known provider must return null.
 */
import { describe, it, expect } from 'vitest';
import { inlineMedia, playerUrl, providerLabel } from './embed';

describe('playerUrl — YouTube', () => {
  it('builds a nocookie embed from a watch URL', () => {
    expect(playerUrl('https://www.youtube.com/watch?v=B0sO1wdBhMY')).toBe(
      'https://www.youtube-nocookie.com/embed/B0sO1wdBhMY',
    );
  });

  it('handles a youtu.be short link', () => {
    expect(playerUrl('https://youtu.be/B0sO1wdBhMY')).toBe(
      'https://www.youtube-nocookie.com/embed/B0sO1wdBhMY',
    );
  });

  it('upgrades an http source to an https player', () => {
    // Real Are.na data carries these — blocks saved before YouTube moved to
    // https keep their original scheme, and an http iframe would be blocked
    // as mixed content.
    expect(playerUrl('http://youtu.be/aIQOozd0kqE')).toBe(
      'https://www.youtube-nocookie.com/embed/aIQOozd0kqE',
    );
  });

  it('handles an already-embed URL', () => {
    expect(playerUrl('https://www.youtube.com/embed/B0sO1wdBhMY')).toBe(
      'https://www.youtube-nocookie.com/embed/B0sO1wdBhMY',
    );
  });

  it('handles a /shorts/ link', () => {
    expect(playerUrl('https://www.youtube.com/shorts/B0sO1wdBhMY')).toBe(
      'https://www.youtube-nocookie.com/embed/B0sO1wdBhMY',
    );
  });

  it('ignores extra query params rather than forwarding them', () => {
    // A forwarded param is attacker-controlled input in an iframe src.
    expect(playerUrl('https://www.youtube.com/watch?v=abc123XYZ_-&t=90&list=x')).toBe(
      'https://www.youtube-nocookie.com/embed/abc123XYZ_-',
    );
  });

  it('rejects a lookalike host', () => {
    expect(playerUrl('https://youtube.com.evil.test/watch?v=abc')).toBeNull();
    expect(playerUrl('https://notyoutube.com/watch?v=abc')).toBeNull();
  });

  it('rejects a video id with unexpected characters', () => {
    expect(playerUrl('https://www.youtube.com/watch?v=abc"><script>')).toBeNull();
  });

  it('accepts the m. and music. subdomains', () => {
    expect(playerUrl('https://m.youtube.com/watch?v=abc123XYZ_-')).toContain('abc123XYZ_-');
  });
});

describe('playerUrl — Vimeo', () => {
  it('builds a player URL from a numeric video page', () => {
    expect(playerUrl('https://vimeo.com/78314194')).toBe('https://player.vimeo.com/video/78314194');
  });

  it('handles an existing player URL', () => {
    expect(playerUrl('https://player.vimeo.com/video/78314194')).toBe(
      'https://player.vimeo.com/video/78314194',
    );
  });

  it('rejects a non-numeric vimeo path', () => {
    expect(playerUrl('https://vimeo.com/channels/staffpicks')).toBeNull();
  });
});

describe('playerUrl — everything else', () => {
  it.each([
    ['an unknown provider', 'https://example.com/video/1'],
    ['a soundcloud link we do not embed', 'https://soundcloud.com/x/y'],
    ['an empty string', ''],
    ['a plain non-URL', 'not a url'],
    ['a javascript: URI', 'javascript:alert(1)'],
    ['a data: URI', 'data:text/html,<script>alert(1)</script>'],
    ['a bare http youtube-looking string', 'ftp://youtube.com/watch?v=abc'],
  ])('returns null for %s', (_label, input) => {
    expect(playerUrl(input)).toBeNull();
  });

  it('is null-safe', () => {
    expect(playerUrl(null)).toBeNull();
    expect(playerUrl(undefined)).toBeNull();
  });
});

describe('providerLabel', () => {
  it('names known providers', () => {
    expect(providerLabel('https://www.youtube.com/watch?v=abc123XYZ_-')).toBe('YouTube');
    expect(providerLabel('https://vimeo.com/78314194')).toBe('Vimeo');
  });

  it('falls back to the hostname for anything else', () => {
    expect(providerLabel('https://example.com/v/1')).toBe('example.com');
  });

  it('is null-safe', () => {
    expect(providerLabel('')).toBe('');
  });
});

describe('inlineMedia', () => {
  it('reads the kind off the stored MIME type', () => {
    expect(inlineMedia('https://a.example/clip.mp4', 'video/mp4')).toEqual({
      url: 'https://a.example/clip.mp4',
      kind: 'video',
    });
    expect(inlineMedia('https://a.example/12.mp3', 'audio/mpeg')?.kind).toBe('audio');
  });

  it('returns the kind and nothing else', () => {
    // `kind` is the whole question — <video> or <audio>. A `type` hint would
    // have no consumer: the elements are single-source and take their format
    // from the response's Content-Type. A field nothing reads is one that
    // drifts, so it is not returned at all.
    expect(Object.keys(inlineMedia('https://a.example/clip.mp4', 'video/mp4')!).sort()).toEqual([
      'kind',
      'url',
    ]);
  });

  it('believes the type over the extension', () => {
    // A server that names its own format is better evidence than a filename,
    // and a mislabelled extension is common on media CDNs.
    expect(inlineMedia('https://a.example/thing.mp3', 'video/mp4')?.kind).toBe('video');
  });

  it('plays a wildcard type the poller wrote for a bare medium', () => {
    // `video/*` is what the poller writes for a `medium` with no type.
    expect(inlineMedia('https://a.example/clip.mp4', 'video/*')?.kind).toBe('video');
  });

  it('falls back to the extension when the feed sent no type', () => {
    expect(inlineMedia('https://a.example/clip.webm', '')?.kind).toBe('video');
    expect(inlineMedia('https://a.example/ep.flac')?.kind).toBe('audio');
  });

  it('ignores the query string when reading an extension', () => {
    expect(inlineMedia('https://a.example/photo.jpg?p=clip.mp4')).toBeNull();
  });

  it.each([
    ['a javascript URL', 'javascript:alert(1)', 'video/mp4'],
    ['a data URL', 'data:video/mp4;base64,AAAA', 'video/mp4'],
    ['a relative path', '/media/clip.mp4', 'video/mp4'],
    ['an unplayable type', 'https://a.example/paper.pdf', 'application/pdf'],
    ['an image', 'https://a.example/photo.jpg', 'image/jpeg'],
    ['an unknown extension with no type', 'https://a.example/thing.xyz', ''],
    ['no extension and no type', 'https://a.example/thing', ''],
    // `.ogg` is a container, not a format: guessing audio would silently drop
    // a Theora video's picture. Absent from both language's tables.
    ['an ambiguous .ogg with no type', 'https://a.example/clip.ogg', ''],
  ])('returns null for %s', (_label, url, type) => {
    expect(inlineMedia(url, type)).toBeNull();
  });

  it.each([
    // The extension fallback must answer exactly as `media_type_for_url`
    // does — see tests/test_feeds_media_parity.py for the table itself.
    ['a leading-dot filename', 'https://a.example/.mp4'],
    ['a trailing slash after the name', 'https://a.example/clip.mp4/'],
    ['a path segment after the name', 'https://a.example/clip.mp4/thumb'],
    ['a name ending in a dot', 'https://a.example/clip.'],
  ])('agrees with the Python parser that %s is not media', (_label, url) => {
    expect(inlineMedia(url)).toBeNull();
  });

  it('strips url path parameters before reading the extension', () => {
    // `;v=1` is part of the path, not of the filename. Both parsers drop it.
    expect(inlineMedia('https://a.example/x.mp4;v=1')?.kind).toBe('video');
  });

  it('is null-safe', () => {
    expect(inlineMedia(null)).toBeNull();
    expect(inlineMedia(undefined)).toBeNull();
    expect(inlineMedia('')).toBeNull();
  });
});
