/**
 * What a location ingest QR code says, and how the phone reads it back.
 *
 * Provisioning is two screens: this page, on a laptop, renders a code; the
 * same page inside the iOS shell, on the phone being tracked, scans it and
 * hands the result to the tracker plugin. Both halves are in this repo, so the
 * payload format has exactly one home and no version skew to negotiate — but
 * it still carries a version, because a scanned code is the one input here
 * that can be arbitrarily old (a screenshot, a printout, a photo of a monitor).
 *
 * **The payload is JSON, deliberately not the webhook URL.** Encoding a bare
 * `https://…?token=…` would make every generic scanner — including the iOS
 * Camera app's built-in detection — offer to open it, putting the token in
 * Safari's address bar and history in exchange for a 405. JSON renders as
 * text, which no scanner offers to do anything with.
 */

/** Bumped only if the shape changes incompatibly. A reader rejects what it does not know. */
export const PROVISIONING_VERSION = 1;

export interface Provisioning {
  /** The bare ingest URL. The token travels in an Authorization header, not here. */
  endpoint: string;
  token: string;
}

/**
 * Nothing legitimate approaches these. They exist so a decoder fed a
 * megabyte of scanned text — or a payload built to be one — spends no time
 * on it. A generated token is 43 characters.
 */
const MAX_PAYLOAD_CHARS = 2048;
const MAX_TOKEN_CHARS = 512;
const MAX_ENDPOINT_CHARS = 512;

/**
 * The URL the server publishes carries a `?token=` the tracker does not use.
 *
 * `/location/settings-info` returns the URL with a `<token>` placeholder for
 * display, and the generate endpoint returns it with the real one filled in.
 * Either way the plugin wants the endpoint on its own, because it sends the
 * token as `Authorization: Bearer` — so the query string is stripped rather
 * than passed along, and the secret appears in the payload exactly once.
 */
export function endpointFromWebhookUrl(webhookUrl: string): string {
  const cut = webhookUrl.indexOf('?');
  return cut === -1 ? webhookUrl : webhookUrl.slice(0, cut);
}

export function encodeProvisioning(p: Provisioning): string {
  return JSON.stringify({ v: PROVISIONING_VERSION, endpoint: p.endpoint, token: p.token });
}

/**
 * Read a scanned string, or return null if it is not one of ours.
 *
 * Everything reaching here came off a camera pointed at whatever happened to
 * be in frame, so this rejects rather than repairs: a wrong version, a
 * plaintext endpoint, a missing field and a merely enormous string all come
 * back null and the card says it did not recognise the code. Refusing http is
 * not belt-and-braces — the plugin refuses it too, and a token posted in the
 * clear is a token to rotate.
 */
export function decodeProvisioning(scanned: string): Provisioning | null {
  if (typeof scanned !== 'string' || scanned.length > MAX_PAYLOAD_CHARS) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(scanned);
  } catch {
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null;

  const { v, endpoint, token } = parsed as Record<string, unknown>;
  if (v !== PROVISIONING_VERSION) return null;
  if (typeof endpoint !== 'string' || typeof token !== 'string') return null;
  if (!endpoint.startsWith('https://') || endpoint.length > MAX_ENDPOINT_CHARS) return null;
  if (!token || token.length > MAX_TOKEN_CHARS) return null;

  return { endpoint, token };
}

/** `example.com` from the endpoint, for the card's "provisioned against" line. */
export function hostOf(endpoint: string): string {
  try {
    return new URL(endpoint).host;
  } catch {
    return endpoint;
  }
}
