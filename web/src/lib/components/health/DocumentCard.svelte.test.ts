import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { HealthDocument } from '$lib/api';
import { viewer } from '$lib/fileViewer/store.svelte';
import FileViewerHost from '$lib/fileViewer/FileViewerHost.svelte';
import DocumentCard from './DocumentCard.svelte';

const doc: HealthDocument = {
  id: 1,
  filename: 'scan.png',
  original_filename: null,
  mime: 'image/png',
  byte_size: 2048,
  source: 'manual',
  notes: null,
  created_at: '2026-06-29T10:00:00Z',
  url: '/istota/api/health/documents/1/file',
  links: [],
};
afterEach(() => {
  cleanup();
  viewer.close();
  vi.restoreAllMocks();
});

describe('Health document image links', () => {
  it('opens the existing viewer host, downloads the original URL and closes', async () => {
    const open = vi.spyOn(viewer, 'openImages');
    render(DocumentCard, { doc });
    render(FileViewerHost);
    const link = screen.getByRole('link', { name: 'scan.png' });
    link.focus();
    const event = new MouseEvent('click', { bubbles: true, cancelable: true });
    await fireEvent(link, event);
    expect(event.defaultPrevented).toBe(true);
    expect(open).toHaveBeenCalledExactlyOnceWith([doc.url], 0);
    expect(document.querySelector('.lightbox img')!.getAttribute('src')).toBe(doc.url);
    const download = screen.getByRole('link', { name: 'Download' });
    expect(download.getAttribute('href')).toBe(doc.url);
    expect(download.getAttribute('download')).toBe('');
    await fireEvent.click(screen.getByRole('button', { name: 'Close image' }));
    expect(document.querySelector('.lightbox img')).toBeNull();
  });

  it.each(['application/pdf', 'text/plain', 'image/svg+xml'])(
    'keeps %s as a download',
    async (mime) => {
      render(DocumentCard, { doc: { ...doc, mime } });
      const event = new MouseEvent('click', { bubbles: true, cancelable: true });
      await fireEvent(screen.getByRole('link', { name: 'scan.png' }), event);
      expect(event.defaultPrevented).toBe(false);
      expect(viewer.state.mode).toBe('closed');
    },
  );

  it.each([
    { ctrlKey: true },
    { metaKey: true },
    { shiftKey: true },
    { altKey: true },
    { button: 1 },
  ])('preserves modified clicks: %o', async (options) => {
    render(DocumentCard, { doc });
    const event = new MouseEvent('click', { bubbles: true, cancelable: true, ...options });
    await fireEvent(screen.getByRole('link', { name: 'scan.png' }), event);
    expect(event.defaultPrevented).toBe(false);
    expect(viewer.state.mode).toBe('closed');
  });

  it('closes the menu before opening its image and preserves modified clicks', async () => {
    render(DocumentCard, { doc });
    render(FileViewerHost);
    await fireEvent.keyDown(screen.getByLabelText('Document actions'), { key: 'Enter' });
    const link = await screen.findByRole('menuitem', { name: 'Open' });
    expect(link.getAttribute('href')).toBe(doc.url);
    await fireEvent.click(link, { ctrlKey: true });
    expect(viewer.state.mode).toBe('closed');
    await fireEvent.keyDown(screen.getByLabelText('Document actions'), { key: 'Enter' });
    const open = await screen.findByRole('menuitem', { name: 'Open' });
    open.focus();
    await fireEvent.click(open);
    expect(viewer.state).toEqual({ mode: 'images', images: [doc.url], index: 0 });
    expect(screen.queryByRole('menu')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Close image' }));
    expect(document.querySelector('.lightbox img')).toBeNull();
    await waitFor(() =>
      expect(document.activeElement === screen.getByLabelText('Document actions')).toBe(true),
    );
  });
});
