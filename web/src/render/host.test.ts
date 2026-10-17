import { describe, it, expect, afterEach, vi } from 'vitest';
import { registerRenderer, type RenderResult } from './host';

type RenderWindow = Window & { istotaRender?: (payload: unknown) => Promise<RenderResult> };

const viewport = { width: 640, height: 360 };

function istotaRender(payload: unknown): Promise<RenderResult> {
  const fn = (window as RenderWindow).istotaRender;
  if (!fn) throw new Error('istotaRender is not installed');
  return fn(payload);
}

afterEach(() => {
  delete (window as RenderWindow).istotaRender;
  document.body.innerHTML = '';
  document.documentElement.removeAttribute('data-theme');
});

describe('registerRenderer', () => {
  it('hands the renderer the payload and a frame sized from its viewport', async () => {
    const seen: { payload?: unknown; frame?: HTMLElement } = {};
    registerRenderer((payload, frame) => {
      seen.payload = payload;
      seen.frame = frame;
      return { ok: true };
    });

    await expect(istotaRender({ viewport, owner: 'Bob' })).resolves.toEqual({ ok: true });
    expect(seen.payload).toEqual({ viewport, owner: 'Bob' });
    expect(seen.frame?.hasAttribute('data-render-frame')).toBe(true);
    expect(seen.frame?.style.width).toBe('640px');
    expect(seen.frame?.style.height).toBe('360px');
    expect(document.querySelectorAll('[data-render-frame]')).toHaveLength(1);
  });

  it('uses the document frame when the shell already has one', async () => {
    document.body.innerHTML = '<div data-render-frame id="shell"></div>';
    let frameId = '';
    registerRenderer((_payload, frame) => {
      frameId = frame.id;
      return { ok: true };
    });
    await istotaRender({ viewport });
    expect(frameId).toBe('shell');
  });

  it('draws light whatever the stored theme', () => {
    document.documentElement.setAttribute('data-theme', 'dark');
    registerRenderer(() => ({ ok: true }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('renders once per document: a second call is busy and draws nothing', async () => {
    const renderer = vi.fn(() => ({ ok: true }) as RenderResult);
    registerRenderer(renderer);

    await istotaRender({ viewport });
    await expect(istotaRender({ viewport })).resolves.toEqual({ ok: false, reason: 'busy' });
    expect(renderer).toHaveBeenCalledTimes(1);
  });

  it('refuses a second registration in the same document', () => {
    registerRenderer(() => ({ ok: true }));
    expect(() => registerRenderer(() => ({ ok: true }))).toThrow();
  });

  it.each([
    ['no payload', null],
    ['an array', [viewport]],
    ['no viewport', { owner: 'Bob' }],
    ['a zero width', { viewport: { width: 0, height: 360 } }],
    ['a fractional height', { viewport: { width: 640, height: 360.5 } }],
    ['an oversized frame', { viewport: { width: 2001, height: 360 } }],
    ['a string size', { viewport: { width: '640', height: 360 } }],
  ])('refuses %s without calling the renderer', async (_label, payload) => {
    const renderer = vi.fn(() => ({ ok: true }) as RenderResult);
    registerRenderer(renderer);
    await expect(istotaRender(payload)).resolves.toEqual({ ok: false, reason: 'bad_payload' });
    expect(renderer).not.toHaveBeenCalled();
  });

  it("passes a renderer's own failure reason through", async () => {
    registerRenderer(async () => ({ ok: false, reason: 'tile_error' }));
    await expect(istotaRender({ viewport })).resolves.toEqual({ ok: false, reason: 'tile_error' });
  });

  it.each([
    ['a thrown error', () => Promise.reject(new Error('https://tiles.example/?key=secret'))],
    ['a reason that is not a code', () => ({ ok: false, reason: 'Failed at 48.2, 16.3' })],
    ['an answer that is not a result', () => 'done'],
  ])('reports %s as render_error', async (_label, renderer) => {
    registerRenderer(renderer as never);
    await expect(istotaRender({ viewport })).resolves.toEqual({
      ok: false,
      reason: 'render_error',
    });
  });
});
