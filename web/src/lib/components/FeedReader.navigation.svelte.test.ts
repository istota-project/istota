import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { FeedEntry } from '$lib/api';
import { clearNotices, currentNotice } from '$lib/stores/notices';
import Fixture from './FeedReaderNavigationFixture.svelte';
import FeedReader from './FeedReader.svelte';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

beforeEach(() => {
  vi.clearAllMocks();
  clearNotices();
});
afterEach(() => {
  cleanup();
  clearNotices();
});

function entry(id: number): FeedEntry {
  return {
    id,
    title: `Article ${id}`,
    url: 'https://example.com/post',
    content: '<p>Article body</p>',
    images: ['/one.jpg', '/two.jpg'],
    duplicate_image_count: 0,
    embed_url: '',
    file_url: '',
    media_url: '',
    media_type: '',
    feed: {
      id: 1,
      title: 'Example feed',
      site_url: 'https://example.com',
      category: { id: 1, title: 'Example category' },
    },
    status: 'unread',
    starred: false,
    starred_at: '',
    published_at: '',
    created_at: '',
  };
}
function deferred() {
  let resolve!: () => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<void>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const reader = () => screen.getByRole('dialog', { name: 'Example feed' });
const next = () => within(reader()).getAllByRole('button', { name: 'Next post' })[0];
async function open() {
  const button = screen.getByRole('button', { name: 'Open reader' });
  button.focus();
  await fireEvent.click(button);
  return button;
}

it('pages once at a loaded boundary, resets scroll, and ignores unrelated updates', async () => {
  const page = deferred();
  const onNeedMore = vi.fn(() => page.promise);
  const onView = vi.fn();
  const entries = [entry(1)];
  const { rerender } = render(Fixture, { entries, hasMore: true, onNeedMore, onView });
  await open();
  const body = reader().querySelector('.ui-modal-body') as HTMLElement;
  body.scrollTop = 240;
  await fireEvent.click(next());
  expect(next()).toBeDisabled();
  await fireEvent.keyDown(reader(), { key: 'ArrowRight' });
  expect(onNeedMore).toHaveBeenCalledTimes(1);
  await rerender({ entries: [...entries, entry(2)] });
  expect(body.scrollTop).toBe(240);
  expect(onView).toHaveBeenCalledTimes(1);
  page.resolve();
  await waitFor(() =>
    expect(within(reader()).getByRole('heading', { name: 'Article 2' })).toBeTruthy(),
  );
  expect(body.scrollTop).toBe(0);
  expect(onView.mock.calls).toEqual([[1], [2]]);
});

it('ignores a page that completes after closing and reopening the same entry', async () => {
  const page = deferred();
  const entries = [entry(1)];
  const { rerender } = render(Fixture, { entries, hasMore: true, onNeedMore: () => page.promise });
  await open();
  await fireEvent.click(next());
  await fireEvent.click(within(reader()).getByRole('button', { name: 'Close' }));
  await open();
  await rerender({ entries: [...entries, entry(2)] });
  page.resolve();
  await waitFor(() => expect(next()).not.toBeDisabled());
  expect(within(reader()).getByRole('heading', { name: 'Article 1' })).toBeTruthy();
});

it('ignores a pending advance after selecting another entry', async () => {
  const page = deferred();
  const entries = [entry(1), entry(2)];
  const { rerender } = render(FeedReader, {
    entries,
    index: 1,
    hasMore: true,
    onNeedMore: () => page.promise,
    onClose: vi.fn(),
  });
  await fireEvent.click(next());
  await rerender({ index: 0, entries: [...entries, entry(3)] });
  page.resolve();
  await waitFor(() => expect(next()).not.toBeDisabled());
  expect(within(reader()).getByRole('heading', { name: 'Article 1' })).toBeTruthy();
});

it('leaves a rejected page retryable without a duplicate failure notice', async () => {
  const page = deferred();
  const onNeedMore = vi.fn().mockReturnValueOnce(page.promise).mockResolvedValue(undefined);
  render(Fixture, { entries: [entry(1)], hasMore: true, onNeedMore });
  await open();
  await fireEvent.click(next());
  page.reject(new Error('Page failed'));
  await waitFor(() => expect(next()).not.toBeDisabled());
  expect(within(reader()).getByRole('heading', { name: 'Article 1' })).toBeTruthy();
  expect(get(currentNotice)).toBeNull();
  await fireEvent.click(next());
  expect(onNeedMore).toHaveBeenCalledTimes(2);
});

it('rolls back a late failed star only on the captured entry', async () => {
  const star = deferred();
  api.updateEntryStarred.mockReturnValue(star.promise);
  const entries = [entry(1), entry(2)];
  render(Fixture, { entries });
  await open();
  await fireEvent.click(screen.getByRole('button', { name: 'Star' }));
  await fireEvent.click(next());
  star.reject(new Error('Star failed'));
  await waitFor(() => expect(get(currentNotice)?.message).toBe("Couldn't update star."));
  expect(entries.map((e) => e.starred)).toEqual([false, false]);
  expect(within(reader()).getByRole('heading', { name: 'Article 2' })).toBeTruthy();
});

it('returns from nested image zoom to the same article, scroll and opener', async () => {
  const onView = vi.fn();
  render(Fixture, { entries: [entry(1), entry(2)], onView });
  const opener = await open();
  const dialog = reader();
  const body = dialog.querySelector('.ui-modal-body') as HTMLElement;
  body.scrollTop = 240;
  const image = dialog.querySelector('.hero-img') as HTMLButtonElement;
  image.focus();
  await fireEvent.click(image);
  const zoom = screen.getByRole('dialog', { name: 'Image viewer' });
  await fireEvent.keyDown(zoom, { key: 'ArrowRight' });
  expect(zoom.querySelector('img')).toHaveAttribute('src', '/two.jpg');
  await fireEvent.keyDown(zoom, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Image viewer' })).toBeNull());
  expect(reader()).toBe(dialog);
  expect(body.scrollTop).toBe(240);
  expect(onView).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(image).toHaveFocus());
  await fireEvent.keyDown(dialog, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await waitFor(() => expect(opener).toHaveFocus());
});

it('renders no dialog for an invalid selection and names a titleless entry', async () => {
  const item = entry(1);
  item.title = '';
  item.feed.title = '';
  const { rerender } = render(FeedReader, { entries: [item], index: 2, onClose: vi.fn() });
  expect(screen.queryByRole('dialog')).toBeNull();
  await rerender({ index: 0 });
  expect(screen.getByRole('dialog', { name: 'Feed entry' })).toBeTruthy();
});
