import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
vi.mock('pdfjs-dist', () => ({ GlobalWorkerOptions: {}, getDocument: vi.fn() }));
import { getDocument } from 'pdfjs-dist';
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));
vi.mock('$lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api')>()),
  previewChatFile: vi.fn(),
}));
import { previewChatFile, chatFileUrl, type FilePreview } from '$lib/api';
import FileViewerHost from './FileViewerHost.svelte';
import { viewer } from './store.svelte';
const preview = vi.mocked(previewChatFile);
const text = (name = 'note.txt', body = 'hello <world>'): FilePreview => ({
  name,
  text: body,
  kind: 'text',
  size: 20,
  modified: '2026-01-01T00:00:00Z',
  truncated: false,
});
beforeEach(() => {
  viewer.close();
  vi.clearAllMocks();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  viewer.close();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
function open(path = '/note.txt') {
  render(FileViewerHost);
  viewer.openFile(path);
}
describe('workspace file viewer through the host', () => {
  it.each(['text', 'image'] as const)(
    'restores the focused opener after %s closes',
    async (kind) => {
      const opener = document.createElement('button');
      document.body.append(opener);
      opener.focus();
      preview.mockResolvedValue({ ...text(), kind });
      open();
      await fireEvent.click(
        await screen.findByRole('button', {
          name: kind === 'image' ? 'Close image' : 'Close',
        }),
      );
      await waitFor(() => expect(document.activeElement).toBe(opener));
      opener.remove();
    },
  );
  it('uses a compact reading shell with header download and a wider source view', async () => {
    preview.mockResolvedValue(text('note.md', '# Short note'));
    open('/note.md');
    await screen.findByRole('heading', { name: 'Short note' });
    const dialog = screen.getByRole('dialog', { name: 'note.md' });
    expect(dialog).toHaveClass('ui-viewer-content');
    expect(dialog.style.getPropertyValue('--modal-height')).toBe('auto');
    expect(dialog.style.getPropertyValue('--modal-width')).toBe('720px');
    const download = screen.getByRole('link', { name: 'Download' });
    expect(download.closest('.ui-viewer-header')).not.toBeNull();
    expect(download).toHaveAttribute('href', chatFileUrl('/note.md'));
    expect(download).toHaveAttribute('download', 'note.md');
    expect(
      screen.getByRole('button', { name: 'Close' }).closest('.ui-viewer-header'),
    ).not.toBeNull();
    expect(dialog.querySelector('.ui-viewer-metadata')).toHaveTextContent('20 B');
    expect(dialog.querySelector('time')).toHaveAttribute('datetime', '2026-01-01T00:00:00Z');
    const body = dialog.querySelector('.ui-modal-body')!;
    body.scrollTop = 80;
    await fireEvent.click(screen.getByRole('button', { name: 'Source' }));
    expect(dialog.style.getPropertyValue('--modal-width')).toBe('960px');
    expect(body.scrollTop).toBe(80);
    await fireEvent.click(screen.getByRole('button', { name: 'Rendered' }));
    expect(dialog.style.getPropertyValue('--modal-width')).toBe('720px');
  });
  it('renders plain text safely', async () => {
    preview.mockResolvedValue(text());
    open();
    const source = await screen.findByRole('textbox', { name: 'Source of note.txt' });
    expect(source).toHaveValue('hello <world>');
    expect(source).toHaveAttribute('readonly');
    expect(source).not.toBeDisabled();
  });
  it('renders markdown, frontmatter, and source', async () => {
    preview.mockResolvedValue(text('note.md', '---\ntitle: Note\n---\n# Hello'));
    open('/note.md');
    expect(await screen.findByRole('heading', { name: 'Hello' })).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Source' }));
    const source = screen.getByRole('textbox', { name: 'Source of note.md' });
    expect(source).toHaveValue('---\ntitle: Note\n---\n# Hello');
    expect(source).toHaveAttribute('readonly');
    expect(screen.queryByRole('heading', { name: 'Hello' })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Rendered' }));
    expect(screen.getByRole('heading', { name: 'Hello' })).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
  });
  it('shows HTML as escaped source', async () => {
    preview.mockResolvedValue(text('note.html', '<script>alert(1)</script>'));
    open('/note.html');
    await waitFor(() => expect(document.querySelector('pre')?.textContent).toContain('<script>'));
    expect(document.querySelector('script')).toBeNull();
  });
  it('keeps binary download available', async () => {
    preview.mockResolvedValue({ ...text(), kind: 'binary', text: undefined });
    open();
    expect(await screen.findByText('No preview for this file type.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Download' }).getAttribute('href')).toBe(
      chatFileUrl('/note.txt'),
    );
  });
  it('renders PDF pages inside the wide shell and releases them on close', async () => {
    const destroy = vi.fn(async () => {});
    const getPage = vi.fn(async () => ({
      getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
      render: () => ({ promise: Promise.resolve(), cancel: vi.fn() }),
      cleanup: vi.fn(),
    }));
    vi.mocked(getDocument).mockReturnValue({
      promise: Promise.resolve({ numPages: 2, getPage }),
      destroy,
    } as never);
    preview.mockResolvedValue({ ...text('report.pdf'), kind: 'pdf', text: undefined });
    open('/report.pdf');
    await screen.findByRole('img', { name: 'PDF page 1' });
    expect(screen.getByRole('dialog').style.getPropertyValue('--modal-width')).toBe('960px');
    expect(getDocument).toHaveBeenCalledWith(
      expect.objectContaining({ url: chatFileUrl('/report.pdf') }),
    );
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Next page' })).not.toBeDisabled(),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await screen.findByRole('img', { name: 'PDF page 2' });
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(destroy).toHaveBeenCalledOnce();
  });
  it('shows truncation', async () => {
    preview.mockResolvedValue({ ...text(), truncated: true });
    open();
    expect(await screen.findByText(/Showing the first/)).toBeTruthy();
  });
  it.each([400, 401, 403, 404])('hides download for a refused file (%s)', async (status) => {
    preview.mockRejectedValue(Object.assign(new Error('File not found'), { status }));
    open();
    expect(await screen.findByText('File not found')).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Download' })).toBeNull();
  });
  it('keeps download on network failure', async () => {
    preview.mockRejectedValue(new TypeError('network'));
    open();
    expect(await screen.findByText("Couldn't load a preview.")).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Download' })).toBeTruthy();
  });
  it('hands images to Lightbox with no remaining file dialog and closes/reopens', async () => {
    preview.mockResolvedValue({ ...text('image.png'), kind: 'image', text: undefined });
    open('/image.png');
    await waitFor(() =>
      expect(document.querySelector('.lightbox img')?.getAttribute('src')).toBe(
        chatFileUrl('/image.png'),
      ),
    );
    expect(screen.queryByRole('dialog', { name: 'Image viewer' })).not.toBeNull();
    expect(document.querySelector('.ui-modal-content')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Close image' }));
    expect(document.querySelector('.lightbox')).toBeNull();
    preview.mockResolvedValue(text());
    viewer.openFile('/note.txt');
    expect(await screen.findByRole('dialog')).toBeTruthy();
    await fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
  it('ignores late results, even after reopening the same path', async () => {
    let resolve!: (v: FilePreview) => void;
    preview.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    open();
    await tick();
    viewer.close();
    await tick();
    preview.mockResolvedValue(text('new.txt', 'fresh'));
    viewer.openFile('/note.txt');
    expect(await screen.findByDisplayValue('fresh')).toBeTruthy();
    resolve(text('old.txt', 'stale'));
    await tick();
    expect(screen.queryByDisplayValue('stale')).toBeNull();
  });
  it('releases playing media when a nested host request replaces it', async () => {
    preview.mockResolvedValueOnce({ ...text('first.mp3'), kind: 'audio', text: undefined });
    open('/first.mp3');
    await waitFor(() => expect(document.querySelector('audio')).not.toBeNull());
    const player = document.querySelector('audio')!;
    preview.mockResolvedValueOnce(text('next.md', '# Next'));
    viewer.openFile('/next.md');
    await screen.findByRole('heading', { name: 'Next' });
    expect(player.pause).toHaveBeenCalled();
    expect(player.load).toHaveBeenCalled();
    expect(player).not.toHaveAttribute('src');
    expect(document.querySelector('audio')).toBeNull();
  });
  it.each(['audio', 'video'] as const)(
    'mounts a controlled %s and provides decode fallback',
    async (kind) => {
      preview.mockResolvedValue({
        ...text('media'),
        kind,
        text: undefined,
        media_type: `${kind}/mp4`,
      });
      open('/media');
      await waitFor(() => expect(document.querySelector(kind)).not.toBeNull());
      const media = document.querySelector(kind)!;
      expect(media.getAttribute('preload')).toBe('metadata');
      expect(media.hasAttribute('controls')).toBe(true);
      expect(media.hasAttribute('autoplay')).toBe(false);
      if (kind === 'video') expect(media.hasAttribute('playsinline')).toBe(true);
      await fireEvent.error(media);
      expect(screen.getByText(/browser could not play/)).toBeTruthy();
      expect(media.pause).toHaveBeenCalled();
      expect(media.load).toHaveBeenCalled();
      expect(media).not.toHaveAttribute('src');
      expect(screen.getByRole('link', { name: 'Download' })).toBeTruthy();
      await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
      expect(document.querySelector(kind)).toBeNull();
    },
  );
});
