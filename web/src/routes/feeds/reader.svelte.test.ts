import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { clearNotices, currentNotice } from '$lib/stores/notices';
import Page from './+page.svelte';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

beforeEach(() => {
  vi.clearAllMocks();
  clearNotices();
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  api.getFeeds.mockResolvedValue({
    entries: [
      {
        id: 1,
        title: 'A gallery',
        url: 'https://example.com/post',
        content: '<p>Gallery body</p>',
        images: ['/one.jpg', '/two.jpg'],
        duplicate_image_count: 0,
        embed_url: '',
        file_url: '',
        media_url: '',
        media_type: '',
        feed: { id: 1, title: 'Example feed', site_url: 'https://example.com', category: null },
        status: 'unread',
        starred: false,
        starred_at: '',
        published_at: '',
        created_at: '',
      },
    ],
    total: 2,
  } as never);
  api.updateEntriesStatus.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clearNotices();
});

async function open() {
  render(Page);
  await screen.findByText('Gallery body');
  const card = document.querySelector('.card-slot [role="button"]') as HTMLElement;
  card.focus();
  await fireEvent.keyDown(card, { key: 'Enter' });
  return { card, reader: screen.getByRole('dialog', { name: 'Example feed' }) };
}

it('opens a card, zooms an image and returns to the same reader and card', async () => {
  const { card, reader } = await open();
  const body = reader.querySelector('.ui-modal-body') as HTMLElement;
  body.scrollTop = 240;
  const image = reader.querySelector('.hero-img') as HTMLButtonElement;
  image.focus();
  await fireEvent.click(image);
  const zoom = screen.getByRole('dialog', { name: 'Image viewer' });
  await fireEvent.keyDown(zoom, { key: 'ArrowRight' });
  expect(zoom.querySelector('img')).toHaveAttribute('src', '/two.jpg');
  await fireEvent.keyDown(zoom, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Image viewer' })).toBeNull());
  expect(screen.getByRole('dialog', { name: 'Example feed' })).toBe(reader);
  expect(body.scrollTop).toBe(240);
  await waitFor(() => expect(image).toHaveFocus());
  expect(api.getFeeds).toHaveBeenCalledTimes(1);
  await fireEvent.keyDown(reader, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await waitFor(() => expect(card).toHaveFocus());
});

it('reports a failed next page once and keeps Next available for retry', async () => {
  const { reader } = await open();
  api.getFeeds.mockRejectedValueOnce(new Error('Offline'));
  await fireEvent.click(within(reader).getAllByRole('button', { name: 'Next post' })[0]);
  await waitFor(() => expect(get(currentNotice)?.message).toBe('Failed to load more feeds.'));
  expect(get(currentNotice)?.count).toBe(1);
  await waitFor(() =>
    expect(within(reader).getAllByRole('button', { name: 'Next post' })[0]).not.toBeDisabled(),
  );
  expect(within(reader).getByRole('heading', { name: 'A gallery' })).toBeTruthy();
});
