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
 * Extensions a browser plays from a plain URL. The TypeScript half of
 * `PLAYABLE_MEDIA_TYPES` in `src/istota/feeds/models.py` — held equal to it by
 * `tests/test_feeds_media_parity.py`, so do not edit one without the other.
 *
 * Consulted only when the feed shipped no MIME type; a stored `media_type` is
 * what normally decides. `.ogg` is deliberately absent — Ogg is a container
 * and `video/ogg` is registered, so guessing audio from it would lose a
 * Theora video's picture silently. `.oga` and `.ogv` are unambiguous.
 */
const PLAYABLE_EXTENSIONS: Record<string, 'video' | 'audio'> = {
  mp4: 'video',
  m4v: 'video',
  mov: 'video',
  webm: 'video',
  ogv: 'video',
  mp3: 'audio',
  m4a: 'audio',
  oga: 'audio',
  opus: 'audio',
  wav: 'audio',
  flac: 'audio',
  aac: 'audio',
};

export interface InlineMedia {
  /** Safe to use as a `<video>` / `<audio>` src. */
  url: string;
  kind: 'video' | 'audio';
}

/**
 * A media file the reader can play inline, or null (ISSUE-356).
 *
 * Same trust posture as `playerUrl` one field over: the return value goes
 * straight into a `src`, so the URL is re-parsed here rather than trusted,
 * and anything that isn't http(s) is refused. Null means "not something we
 * play" — the caller falls back to whatever it did before, never to guessing
 * an element around an unknown URL.
 *
 * `kind` is the whole answer, because it is the whole question: `<video>` or
 * `<audio>`. It comes from the stored MIME type first and the extension only
 * as a fallback. A URL neither of them classifies is not played — an `<audio>`
 * element around an mp4 loses the picture silently, which is a worse failure
 * than the card simply not offering a player. The MIME type itself is not
 * returned: a single-source element takes its format from the response's own
 * `Content-Type`, so a `type` hint would have no consumer, and a field nothing
 * reads is a field that drifts.
 *
 * The extension fallback is deliberately the same rule as the Python side's
 * (`media_type_for_url`): the last path segment, `;params` stripped, a
 * leading-dot filename not treated as an extension.
 */
export function inlineMedia(
  rawUrl: string | null | undefined,
  rawType?: string | null,
): InlineMedia | null {
  const url = parse(rawUrl);
  if (!url) return null;

  const type = (rawType || '').trim().toLowerCase();
  let kind: 'video' | 'audio' | null = null;
  if (type.startsWith('video/')) kind = 'video';
  else if (type.startsWith('audio/')) kind = 'audio';
  else if (!type) kind = extensionKind(url);
  if (!kind) return null;

  return { url: url.href, kind };
}

/** The extension half of `inlineMedia`, matching `media_type_for_url`. */
function extensionKind(url: URL): 'video' | 'audio' | null {
  const segment = url.pathname.split('/').pop()?.split(';')[0] ?? '';
  const dot = segment.lastIndexOf('.');
  // `dot > 0` rather than `>= 0`: ".mp4" is a filename, not an extension.
  if (dot <= 0 || dot === segment.length - 1) return null;
  return PLAYABLE_EXTENSIONS[segment.slice(dot + 1).toLowerCase()] ?? null;
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
