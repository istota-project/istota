import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
vi.mock('pdfjs-dist', () => ({ GlobalWorkerOptions: {}, getDocument: vi.fn() }));
import { getDocument } from 'pdfjs-dist';
import PdfPreview from './PdfPreview.svelte';
import { KEY_ZOOM_STEP, MAX_SCALE } from '$lib/imageZoom';
const load = vi.mocked(getDocument);
const destroy = vi.fn(async () => {});
const cancel = vi.fn();
let renderPage: ReturnType<typeof vi.fn>;
let getPage: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  renderPage = vi.fn(() => ({ promise: Promise.resolve(), cancel }));
  getPage = vi.fn(async () => ({
    getViewport: ({ scale }: { scale: number }) => ({ width: 2000 * scale, height: 3000 * scale }),
    render: renderPage,
    cleanup: vi.fn(),
  }));
  load.mockReturnValue({ promise: Promise.resolve({ numPages: 2, getPage }), destroy } as never);
});
afterEach(cleanup);
describe('PDF lifecycle', () => {
  it('renders bounded canvases, paginates, and destroys the document on close', async () => {
    const view = render(PdfPreview, { url: '/istota/api/chat/files?path=%2Fdoc.pdf' });
    await waitFor(() => expect(renderPage).toHaveBeenCalledOnce());
    const options = load.mock.calls[0][0] as Record<string, unknown>;
    expect(options.url).toBe('/istota/api/chat/files?path=%2Fdoc.pdf');
    expect(options.enableXfa).toBe(false);
    expect(options.useWorkerFetch).toBe(false);
    expect(options.wasmUrl).toMatch(/^\/pdfjs\/[\d.]+\/wasm\/$/);
    expect(options.cMapUrl).toMatch(/^\/pdfjs\/[\d.]+\/cmaps\/$/);
    expect(options.stopAtErrors).toBe(true);
    const canvas = document.querySelector('canvas')!;
    expect(canvas.width * canvas.height).toBeLessThanOrEqual(8_000_000);
    expect(Math.max(canvas.width, canvas.height)).toBeLessThanOrEqual(4096);
    await fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(getPage).toHaveBeenLastCalledWith(2));
    view.unmount();
    expect(destroy).toHaveBeenCalledOnce();
  });
  it('cancels an active render when closed', async () => {
    renderPage.mockReturnValue({ promise: new Promise(() => {}), cancel });
    const view = render(PdfPreview, { url: '/doc.pdf' });
    await waitFor(() => expect(renderPage).toHaveBeenCalled());
    view.unmount();
    expect(cancel).toHaveBeenCalled();
    expect(destroy).toHaveBeenCalled();
  });
  it.each([
    ['PasswordException', /password/],
    ['InvalidPDFException', /invalid/],
    ['UnknownErrorException', /Couldn.t load/],
  ])('shows readable %s failures', async (name, message) => {
    load.mockReturnValue({
      promise: Promise.reject(Object.assign(new Error('internal'), { name })),
      destroy,
    } as never);
    render(PdfPreview, { url: '/doc.pdf' });
    expect(await screen.findByText(message)).toBeTruthy();
  });
  it.each([
    ['ResponseException', 404, 404],
    ['InvalidPDFException', undefined, null],
  ])('reports a %s load failure with its HTTP status', async (name, status, expected) => {
    load.mockReturnValue({
      promise: Promise.reject(Object.assign(new Error('internal'), { name, status })),
      destroy,
    } as never);
    const onLoadError = vi.fn();
    render(PdfPreview, { url: '/doc.pdf', onLoadError });
    await waitFor(() => expect(onLoadError).toHaveBeenCalledExactlyOnceWith(expected));
  });
});

/**
 * jsdom has no PointerEvent; a MouseEvent carrying the pointer fields is what
 * the handlers read, as in the Lightbox tests.
 */
function pointer(type: string, id: number, x: number, y: number): MouseEvent {
  const e = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y });
  Object.defineProperty(e, 'pointerId', { value: id });
  Object.defineProperty(e, 'pointerType', { value: 'touch' });
  return e;
}

/** A touch event with the given fingers down; jsdom has no Touch constructor. */
function touch(type: string, points: Array<[number, number]>): Event {
  const e = new Event(type, { bubbles: true, cancelable: true });
  const list = points.map(([clientX, clientY], identifier) => ({ identifier, clientX, clientY }));
  Object.defineProperty(e, 'touches', { value: list });
  return e;
}

/** A tap on the drawn page, which is where a tap counts. */
async function tap(x = 200, y = 300): Promise<void> {
  const canvases = document.querySelectorAll('canvas');
  const el = canvases[canvases.length - 1];
  el.dispatchEvent(pointer('pointerdown', 1, x, y));
  el.dispatchEvent(pointer('pointerup', 1, x, y));
  await tick();
}

/** The page's laid-out width in px. jsdom lays out nothing, so fit is the 800px fallback. */
function pageWidth(): number {
  const canvases = document.querySelectorAll('canvas');
  return parseFloat(canvases[canvases.length - 1].style.width);
}

async function open() {
  const view = render(PdfPreview, { url: '/doc.pdf' });
  await waitFor(() => expect(renderPage).toHaveBeenCalledOnce());
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
  return { view, page: screen.getByRole('region', { name: 'PDF page' }) };
}

/** Wait for the page to settle at a laid-out width, with its render finished. */
async function settledAt(width: number) {
  await waitFor(() => {
    expect(document.querySelectorAll('canvas')).toHaveLength(1);
    expect(pageWidth()).toBeCloseTo(width);
  });
}

describe('PDF page zoom', () => {
  it('zooms on a double tap and returns to fit on the next one', async () => {
    await open();
    await tap();
    await tap();
    await settledAt(2000);
    // Drawn again at the new size: 2000 css px of a 2000pt page is scale 1.
    expect(renderPage.mock.calls.at(-1)![0].viewport.width).toBe(2000);
    await tap();
    await tap();
    await settledAt(800);
  });

  it('does not take a double press on the scrollbar for a double tap', async () => {
    const { page } = await open();
    for (let i = 0; i < 2; i++) {
      page.dispatchEvent(pointer('pointerdown', 1, 795, 300));
      page.dispatchEvent(pointer('pointerup', 1, 795, 300));
    }
    await new Promise((r) => setTimeout(r, 50));
    expect(renderPage).toHaveBeenCalledOnce();
  });

  it('does not zoom on a single tap', async () => {
    await open();
    await tap();
    await new Promise((r) => setTimeout(r, 50));
    expect(pageWidth()).toBe(800);
    expect(renderPage).toHaveBeenCalledOnce();
  });

  it('steps with the keyboard and resets to fit', async () => {
    const { page } = await open();
    await fireEvent.keyDown(page, { key: '+' });
    await settledAt(800 * KEY_ZOOM_STEP);
    await fireEvent.keyDown(page, { key: '-' });
    await settledAt(800);
    await fireEvent.keyDown(page, { key: '=' });
    await fireEvent.keyDown(page, { key: '=' });
    await settledAt(800 * KEY_ZOOM_STEP ** 2);
    await fireEvent.keyDown(page, { key: '0' });
    await settledAt(800);
  });

  it('leaves the browser zoom shortcut alone', async () => {
    const { page } = await open();
    const event = new KeyboardEvent('keydown', { key: '+', ctrlKey: true, cancelable: true });
    page.dispatchEvent(event);
    await tick();
    expect(event.defaultPrevented).toBe(false);
    expect(pageWidth()).toBe(800);
  });

  it('zooms from the page controls', async () => {
    await open();
    const zoomOut = screen.getByRole('button', { name: 'Zoom out' }) as HTMLButtonElement;
    expect(zoomOut.disabled).toBe(true);
    await fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    await settledAt(800 * KEY_ZOOM_STEP);
    await fireEvent.click(zoomOut);
    await settledAt(800);
  });

  it('keeps a deep zoom inside the scale range and the canvas caps', async () => {
    const { page } = await open();
    for (let i = 0; i < 12; i++) await fireEvent.keyDown(page, { key: '+' });
    await settledAt(800 * MAX_SCALE);
    expect((screen.getByRole('button', { name: 'Zoom in' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    const canvas = document.querySelector('canvas')!;
    expect(Math.max(canvas.width, canvas.height)).toBeLessThanOrEqual(4096);
    expect(canvas.width * canvas.height).toBeLessThanOrEqual(8_000_000);
  });

  it('keeps the drawn page on screen until the zoomed one is ready', async () => {
    const { page } = await open();
    const first = document.querySelector('canvas')!;
    let finish!: () => void;
    renderPage.mockReturnValueOnce({ promise: new Promise<void>((r) => (finish = r)), cancel });
    await fireEvent.keyDown(page, { key: '+' });
    await waitFor(() => expect(renderPage).toHaveBeenCalledTimes(2));
    // Scaled up by css in the meantime, with no blank canvas and no loading line.
    expect(first.isConnected).toBe(true);
    expect(document.querySelectorAll('canvas')).toHaveLength(1);
    expect(parseFloat(first.style.width)).toBeCloseTo(800 * KEY_ZOOM_STEP);
    expect(screen.queryByRole('status')).toBeNull();
    finish();
    await waitFor(() => expect(first.isConnected).toBe(false));
    expect(document.querySelectorAll('canvas')).toHaveLength(1);
  });

  it('zooms by the spread of a pinch, and only once the fingers lift', async () => {
    const { page } = await open();
    page.dispatchEvent(
      touch('touchstart', [
        [150, 300],
        [250, 300],
      ]),
    );
    const move = touch('touchmove', [
      [100, 300],
      [300, 300],
    ]);
    page.dispatchEvent(move);
    await tick();
    expect(move.defaultPrevented).toBe(true);
    // Previewed by a transform, not laid out or drawn again mid-gesture.
    expect(pageWidth()).toBe(800);
    expect(document.querySelector('canvas')!.style.transform).toMatch(/scale\(2\)/);
    expect(renderPage).toHaveBeenCalledOnce();
    page.dispatchEvent(touch('touchend', [[300, 300]]));
    await settledAt(1600);
    expect(document.querySelector('canvas')!.style.transform).toBe('');
  });

  it('leaves a one-finger drag to the browser', async () => {
    const { page } = await open();
    page.dispatchEvent(touch('touchstart', [[150, 300]]));
    const move = touch('touchmove', [[150, 200]]);
    page.dispatchEvent(move);
    expect(move.defaultPrevented).toBe(false);
  });

  it('zooms on a trackpad pinch once the burst ends, and ignores a plain scroll', async () => {
    const { page } = await open();
    const scroll = new WheelEvent('wheel', { deltaY: 40, cancelable: true, bubbles: true });
    page.dispatchEvent(scroll);
    expect(scroll.defaultPrevented).toBe(false);
    const zoom = new WheelEvent('wheel', {
      deltaY: -30,
      ctrlKey: true,
      cancelable: true,
      bubbles: true,
    });
    page.dispatchEvent(zoom);
    expect(zoom.defaultPrevented).toBe(true);
    await settledAt(800 * Math.exp(30 / 120));
  });

  it('keeps the zoom across pages and resets it for a new document', async () => {
    const { view, page } = await open();
    await fireEvent.keyDown(page, { key: '+' });
    await settledAt(800 * KEY_ZOOM_STEP);
    await fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(getPage).toHaveBeenLastCalledWith(2));
    await settledAt(800 * KEY_ZOOM_STEP);
    await view.rerender({ url: '/other.pdf' });
    await settledAt(800);
  });

  it('finishes loading when a zoom lands before the first draw does', async () => {
    renderPage.mockReturnValueOnce({ promise: new Promise<void>(() => {}), cancel });
    render(PdfPreview, { url: '/doc.pdf' });
    await waitFor(() => expect(renderPage).toHaveBeenCalledOnce());
    await fireEvent.keyDown(screen.getByRole('region', { name: 'PDF page' }), { key: '+' });
    await settledAt(800 * KEY_ZOOM_STEP);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it("drops the last document's page while the next one loads", async () => {
    const { view } = await open();
    const old = document.querySelector('canvas')!;
    load.mockReturnValueOnce({ promise: new Promise(() => {}), destroy } as never);
    await view.rerender({ url: '/other.pdf' });
    expect(old.isConnected).toBe(false);
    expect(screen.getByRole('status')).toBeTruthy();
  });
});
