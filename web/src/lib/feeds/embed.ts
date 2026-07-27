/**
 * Turn a stored `embed_url` (a canonical watch page) into a player URL.
 *
 * The backend deliberately never stores a provider's `<iframe>` — allowing
 * iframes through the feed sanitizer would loosen it for every RSS feed, and
 * Are.na often wraps its embeds in a third-party `cdn.embedly.com` document.
 * The cost of that choice is that the reader rebuilds the player, which makes
 * this module a trust boundary: its return value is used directly as an
 * iframe `src`.
 *
 * So it is an allowlist, not a parser. A URL must match a known provider on an
 * exact host, and the id is re-extracted and re-validated against a strict
 * character class rather than passed through — no query string from the
 * original URL ever reaches the player.
 */

/** Video ids are opaque tokens; anything outside this class is not one. */
const YOUTUBE_ID = /^[A-Za-z0-9_-]{6,20}$/;
const VIMEO_ID = /^\d{6,12}$/;

const YOUTUBE_HOSTS = new Set([
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
  'youtube-nocookie.com',
  'www.youtube-nocookie.com',
]);

const YOUTU_BE_HOSTS = new Set(['youtu.be', 'www.youtu.be']);

const VIMEO_HOSTS = new Set(['vimeo.com', 'www.vimeo.com', 'player.vimeo.com']);

function parse(raw: string | null | undefined): URL | null {
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  // Blocks javascript:, data: and friends before host matching even runs.
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
  return url;
}

/** The last non-empty path segment, e.g. `/embed/abc/` → `abc`. */
function lastSegment(url: URL): string {
  const parts = url.pathname.split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : '';
}

function youtubeId(url: URL): string | null {
  const host = url.hostname.toLowerCase();

  if (YOUTU_BE_HOSTS.has(host)) {
    const id = lastSegment(url);
    return YOUTUBE_ID.test(id) ? id : null;
  }

  if (!YOUTUBE_HOSTS.has(host)) return null;

  // /watch?v=ID
  const v = url.searchParams.get('v');
  if (v) return YOUTUBE_ID.test(v) ? v : null;

  // /embed/ID, /shorts/ID, /v/ID
  const parts = url.pathname.split('/').filter(Boolean);
  if (parts.length >= 2 && ['embed', 'shorts', 'v'].includes(parts[0])) {
    return YOUTUBE_ID.test(parts[1]) ? parts[1] : null;
  }
  return null;
}

function vimeoId(url: URL): string | null {
  const host = url.hostname.toLowerCase();
  if (!VIMEO_HOSTS.has(host)) return null;
  // vimeo.com/78314194 and player.vimeo.com/video/78314194 both end in the id.
  const id = lastSegment(url);
  return VIMEO_ID.test(id) ? id : null;
}

/**
 * A same-origin-safe player URL for a known provider, or null.
 *
 * Null means "we can't play this here" — the caller should link out instead of
 * guessing, since a guessed iframe src is the failure mode this guards.
 */
export function playerUrl(raw: string | null | undefined): string | null {
  const url = parse(raw);
  if (!url) return null;

  const yt = youtubeId(url);
  // -nocookie is YouTube's no-tracking-until-play host; the reader is a
  // private feed, not a page that should be reporting to Google on scroll.
  if (yt) return `https://www.youtube-nocookie.com/embed/${yt}`;

  const vimeo = vimeoId(url);
  if (vimeo) return `https://player.vimeo.com/video/${vimeo}`;

  return null;
}

/**
 * Short uppercase format name for an attached file, e.g. `PDF`.
 *
 * Read from the URL path, never the query — Are.na cache-busts attachments
 * with a bare `?1776186521`, which would otherwise become the badge. Falls
 * back to `FILE` rather than guessing, so an extensionless upload still gets
 * a badge that reads as deliberate.
 */
export function fileKind(raw: string | null | undefined): string {
  const url = parse(raw);
  if (!url) return '';
  const name = lastSegment(url);
  const dot = name.lastIndexOf('.');
  if (dot <= 0 || dot === name.length - 1) return 'FILE';
  const ext = name.slice(dot + 1);
  // A long or non-alphanumeric tail isn't a format name.
  return /^[A-Za-z0-9]{1,5}$/.test(ext) ? ext.toUpperCase() : 'FILE';
}

/** Human name for the source, for the play button's label. */
export function providerLabel(raw: string | null | undefined): string {
  const url = parse(raw);
  if (!url) return '';
  if (youtubeId(url)) return 'YouTube';
  if (vimeoId(url)) return 'Vimeo';
  return url.hostname.replace(/^www\./, '');
}
