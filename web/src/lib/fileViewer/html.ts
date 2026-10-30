/**
 * An HTML file is drawn in an iframe with no `allow-scripts` and no
 * `allow-same-origin`: an opaque origin that runs no script, submits no form
 * and cannot reach the app's DOM, cookies or storage. The page's own styles
 * stay inside the frame. The policy below adds what the sandbox does not
 * stop, any load from the network (a tracking pixel, a remote stylesheet);
 * a second policy the page declares can only narrow it.
 */
export const HTML_FRAME_SANDBOX = 'allow-popups allow-popups-to-escape-sandbox';

export const HTML_FRAME_CSP =
  "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:";

const LEADING_DOCTYPE = /^﻿?\s*<!doctype[^>]*>/i;

/** The page as the frame's `srcdoc`, with the policy ahead of anything it loads. */
export function sandboxedDocument(text: string): string {
  // Links open in a new tab, as a Markdown document's do, rather than
  // navigating the frame away from the file.
  const head = `<meta http-equiv="Content-Security-Policy" content="${HTML_FRAME_CSP}"><base target="_blank">`;
  // After a doctype, not before it: anything ahead of it puts the page in quirks mode.
  const doctype = LEADING_DOCTYPE.exec(text);
  if (!doctype) return head + text;
  return doctype[0] + head + text.slice(doctype[0].length);
}

/**
 * A relative `<img src>` or stylesheet `<link href>` is fetched by the viewer
 * through the endpoint it opened the page from, so the server decides scope
 * per file, and inlined: an image as a `data:` URI, a stylesheet as `<style>`.
 * The frame itself still loads nothing.
 */
export const MAX_ASSETS = 40;
export const MAX_ASSET_BYTES = 5 * 1024 * 1024;
export const MAX_TOTAL_BYTES = 20 * 1024 * 1024;
const FETCH_CONCURRENCY = 4;
// What `/chat/files` sniffs as a raster; anything else stays unresolved.
const RASTER_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);

/** The path `ref` names beside the document at `docPath`, or null if it is not a plain relative file path. */
export function resolveRelative(docPath: string, ref: string): string | null {
  if (!ref || ref.startsWith('/') || ref.startsWith('#') || ref.startsWith('?')) return null;
  // A URL parser reads a backslash as a slash, so `\\host` would be protocol-relative.
  if (ref.includes('\\') || /^[a-z][a-z0-9+.-]*:/i.test(ref)) return null;
  const bare = ref.split(/[?#]/, 1)[0];
  const parts = docPath.split('/').filter(Boolean).slice(0, -1);
  const pieces = bare.split('/');
  if (pieces.at(-1) === '') return null;
  for (const piece of pieces) {
    let segment: string;
    try {
      segment = decodeURIComponent(piece);
    } catch {
      return null;
    }
    if (segment.includes('/')) return null;
    if (segment === '' || segment === '.') continue;
    if (segment === '..') {
      if (parts.length === 0) return null;
      parts.pop();
      continue;
    }
    parts.push(segment);
  }
  if (parts.length === 0) return null;
  return (docPath.startsWith('/') ? '/' : '') + parts.join('/');
}

type Asset = { kind: 'image' | 'css'; path: string };

async function readCapped(response: Response, max: number): Promise<Uint8Array | null> {
  const declared = Number(response.headers.get('Content-Length'));
  if (declared > max || !response.body) {
    await response.body?.cancel();
    return null;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return bytes;
}

function base64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

async function fetchAsset(
  asset: Asset,
  urlFor: (path: string) => string,
  signal: AbortSignal | undefined,
): Promise<{ value: string; size: number } | null> {
  const response = await fetch(urlFor(asset.path), { credentials: 'same-origin', signal });
  if (!response.ok) {
    await response.body?.cancel();
    return null;
  }
  const type = (response.headers.get('Content-Type') ?? '').split(';')[0].trim().toLowerCase();
  if (asset.kind === 'image' && !RASTER_TYPES.has(type)) {
    await response.body?.cancel();
    return null;
  }
  const bytes = await readCapped(response, MAX_ASSET_BYTES);
  if (!bytes) return null;
  if (asset.kind === 'image')
    return { value: `data:${type};base64,${base64(bytes)}`, size: bytes.length };
  return { value: new TextDecoder().decode(bytes), size: bytes.length };
}

/** The page as the frame's `srcdoc`, its relative images and stylesheets inlined. */
export async function frameDocument(
  text: string,
  docPath: string,
  urlFor: (path: string) => string,
  signal?: AbortSignal,
): Promise<string> {
  // A parsed document is inert: no script runs and nothing loads.
  const doc = new DOMParser().parseFromString(text, 'text/html');
  const images = [...doc.querySelectorAll('img[src]')].flatMap((element) => {
    const path = resolveRelative(docPath, element.getAttribute('src')!);
    return path ? [{ element, asset: { kind: 'image', path } as Asset }] : [];
  });
  const sheets = [...doc.querySelectorAll('link[href]')].flatMap((element) => {
    const rel = (element.getAttribute('rel') ?? '').toLowerCase().split(/\s+/);
    const path = rel.includes('stylesheet')
      ? resolveRelative(docPath, element.getAttribute('href')!)
      : null;
    return path ? [{ element, asset: { kind: 'css', path } as Asset }] : [];
  });
  if (images.length === 0 && sheets.length === 0) return sandboxedDocument(text);

  const key = (asset: Asset) => `${asset.kind}:${asset.path}`;
  const wanted = new Map<string, Asset>();
  for (const { asset } of [...sheets, ...images]) {
    if (wanted.size >= MAX_ASSETS) break;
    wanted.set(key(asset), asset);
  }
  const queue = [...wanted.values()];
  const loaded = new Map<string, string>();
  let total = 0;
  async function worker() {
    for (let asset = queue.shift(); asset; asset = queue.shift()) {
      if (total >= MAX_TOTAL_BYTES) return;
      const result = await fetchAsset(asset, urlFor, signal).catch((e: unknown) => {
        if (signal?.aborted) throw e;
        return null;
      });
      if (!result || total + result.size > MAX_TOTAL_BYTES) continue;
      total += result.size;
      loaded.set(key(asset), result.value);
    }
  }
  await Promise.all(Array.from({ length: FETCH_CONCURRENCY }, worker));

  for (const { element, asset } of images) {
    const value = loaded.get(key(asset));
    if (value === undefined) continue;
    element.setAttribute('src', value);
    // A srcset would win over src and name files the frame cannot load.
    element.removeAttribute('srcset');
    element.removeAttribute('sizes');
  }
  for (const { element, asset } of sheets) {
    const value = loaded.get(key(asset));
    if (value === undefined) continue;
    const style = doc.createElement('style');
    // Style text is serialized raw, so a closing tag in it would end the element.
    style.textContent = value.replace(/<\/style/gi, '<\\/style');
    element.replaceWith(style);
  }
  const serialized = (doc.doctype ? '<!DOCTYPE html>' : '') + doc.documentElement.outerHTML;
  return sandboxedDocument(serialized);
}
