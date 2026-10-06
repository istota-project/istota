import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));
vi.mock('$lib/api', async (original) => ({
  ...(await original<typeof import('$lib/api')>()),
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
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  viewer.close();
  vi.restoreAllMocks();
});
function open(path = '/note.txt') {
  render(FileViewerHost);
  viewer.openFile(path);
}
describe('workspace file viewer through the host', () => {
  it('renders plain text safely', async () => {
    preview.mockResolvedValue(text());
    open();
    await waitFor(() => expect(document.querySelector('pre')?.textContent).toBe('hello <world>'));
  });
  it('renders markdown, frontmatter, and source', async () => {
    preview.mockResolvedValue(text('note.md', '---\ntitle: Note\n---\n# Hello'));
    open('/note.md');
    expect(await screen.findByRole('heading', { name: 'Hello' })).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Source' }));
    expect(document.querySelector('pre')?.textContent).toContain('# Hello');
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
  it('shows truncation', async () => {
    preview.mockResolvedValue({ ...text(), truncated: true });
    open();
    expect(await screen.findByText(/Showing the first/)).toBeTruthy();
  });
  it('hides download for a refused file', async () => {
    preview.mockRejectedValue(Object.assign(new Error('File not found'), { status: 404 }));
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
  it('hands images to Lightbox with no remaining dialog and closes/reopens', async () => {
    preview.mockResolvedValue({ ...text('image.png'), kind: 'image', text: undefined });
    open('/image.png');
    await waitFor(() =>
      expect(document.querySelector('.lightbox img')?.getAttribute('src')).toBe(
        chatFileUrl('/image.png'),
      ),
    );
    expect(screen.queryByRole('dialog')).toBeNull();
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
    expect(await screen.findByText('fresh')).toBeTruthy();
    resolve(text('old.txt', 'stale'));
    await tick();
    expect(screen.queryByText('stale')).toBeNull();
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
      expect(screen.getByRole('link', { name: 'Download' })).toBeTruthy();
      await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
      expect(document.querySelector(kind)).toBeNull();
    },
  );
});
