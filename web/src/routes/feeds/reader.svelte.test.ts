import { SearchDialog } from '$lib/components/ui';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import { selectedFeedId } from '$lib/stores/feeds';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { clearNotices, currentNotice } from '$lib/stores/notices';
import Page from './+page.svelte';
import { __history } from '../../../vitest-stubs/app-navigation';
import { page } from '../../../vitest-stubs/app-state.svelte';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

beforeEach(() => {
  vi.clearAllMocks();
  __history.reset('/istota/feeds/');
  selectedFeedId.set(0);
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

it('ignores a previous feed pagination response after a selection change', async () => {
  const { reader } = await open();
  const firstPage = await api.getFeeds.mock.results[0].value;
  let finish!: (value: typeof firstPage) => void;
  api.getFeeds.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  await fireEvent.click(within(reader).getAllByRole('button', { name: 'Next post' })[0]);
  await waitFor(() => expect(api.getFeeds).toHaveBeenCalledTimes(2));
  await fireEvent.keyDown(reader, { key: 'Escape' });
  api.getFeeds.mockResolvedValue({
    entries: [{ ...firstPage.entries[0], id: 2, content: '<p>New feed body</p>' }],
    total: 1,
  });
  selectedFeedId.set(2);
  await screen.findByText('New feed body');
  finish({
    entries: [{ ...firstPage.entries[0], id: 3, content: '<p>Old feed page</p>' }],
    total: 2,
  });
  await tick();
  await tick();
  expect(screen.getByText('New feed body')).toBeTruthy();
  expect(screen.queryByText('Old feed page')).toBeNull();
});

it('keeps search typing and chip shortcuts away from the feeds listener', async () => {
  await open();
  api.updateEntriesStatus.mockClear();
  api.updateEntryStarred.mockClear();
  render(SearchDialog, { open: true });
  const input = screen.getByRole('combobox');
  input.focus();
  expect(input).toHaveFocus();
  await fireEvent.keyDown(input, { key: 'f' });
  await fireEvent.keyDown(input, { key: 'A', shiftKey: true });
  const chip = screen.getByRole('button', { name: 'All' });
  chip.focus();
  await fireEvent.keyDown(chip, { key: 'A', shiftKey: true });
  expect(api.updateEntriesStatus).not.toHaveBeenCalled();
  expect(api.updateEntryStarred).not.toHaveBeenCalled();
});

it('opens a loaded entry from its URL without fetching it again', async () => {
  __history.reset('/istota/feeds/?entry=1');
  render(Page);
  const reader = await screen.findByRole('dialog', { name: 'Example feed' });
  expect(within(reader).getByRole('heading', { name: 'A gallery' })).toBeTruthy();
  await fireEvent.keyDown(reader, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(window.location.search).toBe('');
});

it('fetches an older search hit on same-route navigation without adding it to the list', async () => {
  render(Page);
  await screen.findByText('Gallery body');
  const first = (await api.getFeeds.mock.results[0].value).entries[0];
  api.getFeedEntry = vi
    .fn()
    .mockResolvedValue({ ...first, id: 90, title: 'Older article', content: '<p>Old body</p>' });
  window.history.replaceState(null, '', '/istota/feeds/?entry=90');
  page.url = new URL(window.location.href);
  page.state = {};
  const reader = await screen.findByRole('dialog', { name: 'Example feed' });
  expect(within(reader).getByRole('heading', { name: 'Older article' })).toBeTruthy();
  expect(api.getFeedEntry).toHaveBeenCalledWith(90);
  expect(document.querySelectorAll('.card-slot')).toHaveLength(1);
  expect(within(reader).getAllByRole('button', { name: 'Next post' })[0]).toBeDisabled();
});

it('ignores an older entry response after the URL target changes', async () => {
  render(Page);
  await screen.findByText('Gallery body');
  const first = (await api.getFeeds.mock.results[0].value).entries[0];
  let finish!: (entry: typeof first) => void;
  api.getFeedEntry = vi.fn().mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  window.history.replaceState(null, '', '/istota/feeds/?entry=90');
  page.url = new URL(window.location.href);
  page.state = {};
  await waitFor(() => expect(api.getFeedEntry).toHaveBeenCalledWith(90));
  window.history.replaceState(null, '', '/istota/feeds/?entry=1');
  page.url = new URL(window.location.href);
  await screen.findByRole('dialog', { name: 'Example feed' });
  finish({ ...first, id: 90, title: 'Stale article' });
  await tick();
  await tick();
  expect(screen.queryByRole('heading', { name: 'Stale article' })).toBeNull();
  expect(screen.getByRole('heading', { name: 'A gallery' })).toBeTruthy();
});
