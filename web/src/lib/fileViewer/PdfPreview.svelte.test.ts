import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
vi.mock('pdfjs-dist', () => ({ GlobalWorkerOptions: {}, getDocument: vi.fn() }));
import { getDocument } from 'pdfjs-dist';
import PdfPreview from './PdfPreview.svelte';
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
});
