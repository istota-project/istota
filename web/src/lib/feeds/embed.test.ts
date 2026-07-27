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
import { playerUrl, providerLabel } from './embed';

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
