// The contract between a render bundle and the browser service's
// `POST /render-document` (docker/browser/document_render.py).
//
// The service serves a bundle as a self-contained document on its own render
// origin, calls `window.istotaRender(payload)` once, waits for the promise,
// and screenshots `[data-render-frame]`. The page never calls istota: no API,
// no cookie, no callback origin. Everything it draws is in the payload.
//
// A renderer is an entry under `src/render/<name>/main.ts` that calls
// `registerRenderer` with a function drawing the payload into the frame,
// usually by mounting real app components with Svelte's `mount`, so the image
// looks like the web UI. `vite.render.config.ts` builds each entry into
// `build-render/<name>.html`.

import '$lib/styles/tokens.css';
import '$lib/styles/app-tokens.css';
import './host.css';
import { applyTheme } from '$lib/stores/theme';

export type RenderResult = { ok: true } | { ok: false; reason: string };

export type Renderer = (
  payload: Record<string, unknown>,
  frame: HTMLElement,
) => RenderResult | Promise<RenderResult>;

type RenderWindow = Window & {
  istotaRender?: (payload: unknown) => Promise<RenderResult>;
};

/** The largest frame the service will capture, in CSS px, either side. */
export const MAX_VIEWPORT = 2000;

const REASON = /^[a-z_]{1,40}$/;

function viewportOf(payload: Record<string, unknown>): { width: number; height: number } | null {
  const raw = payload.viewport;
  if (!raw || typeof raw !== 'object') return null;
  const { width, height } = raw as Record<string, unknown>;
  const fits = (v: unknown): v is number =>
    typeof v === 'number' && Number.isInteger(v) && v >= 1 && v <= MAX_VIEWPORT;
  return fits(width) && fits(height) ? { width, height } : null;
}

function frameIn(doc: Document): HTMLElement {
  const existing = doc.querySelector<HTMLElement>('[data-render-frame]');
  if (existing) return existing;
  const frame = doc.createElement('div');
  frame.setAttribute('data-render-frame', '');
  doc.body.appendChild(frame);
  return frame;
}

/** A renderer's own reason, or `render_error` when it is not one the service passes on. */
function settled(result: unknown): RenderResult {
  if (result && typeof result === 'object' && (result as RenderResult).ok === true) {
    return { ok: true };
  }
  const reason = (result as { reason?: unknown } | null)?.reason;
  return {
    ok: false,
    reason: typeof reason === 'string' && REASON.test(reason) ? reason : 'render_error',
  };
}

/**
 * Install `window.istotaRender` for this document. One render per document:
 * the service opens a fresh context for each, so a second call is refused as
 * `busy` rather than drawn over the first.
 */
export function registerRenderer(fn: Renderer, target: Window = window): void {
  const win = target as RenderWindow;
  if (typeof win.istotaRender === 'function') {
    throw new Error('a renderer is already registered in this document');
  }
  // Images are drawn light: the app's pre-paint default is dark, and a
  // picture posted into a chat has no viewer theme to follow.
  applyTheme('light');
  let used = false;
  win.istotaRender = async (raw: unknown): Promise<RenderResult> => {
    if (used) return { ok: false, reason: 'busy' };
    used = true;
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      return { ok: false, reason: 'bad_payload' };
    }
    const payload = raw as Record<string, unknown>;
    const viewport = viewportOf(payload);
    if (!viewport) return { ok: false, reason: 'bad_payload' };
    const frame = frameIn(target.document);
    frame.style.width = `${viewport.width}px`;
    frame.style.height = `${viewport.height}px`;
    try {
      return settled(await fn(payload, frame));
    } catch {
      return { ok: false, reason: 'render_error' };
    }
  };
}
