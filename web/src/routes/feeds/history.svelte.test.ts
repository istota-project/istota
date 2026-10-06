import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import { __history, goto } from '$app/navigation';
import { page } from '$app/state';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$app/navigation', async (original) => ({
  ...(await original<typeof import('$app/navigation')>()),
  goto: vi.fn(async () => undefined),
}));
await fillApiDouble(api);

import { getFeeds, type Feed, type FeedEntry, type User } from '$lib/api';
import {
  feedsList,
  selectedFeedId,
  selectedCategoryId,
  showStarred,
  showUnseen,
  feedsRefreshNonce,
} from '$lib/stores/feeds';
import Layout from './+layout.svelte';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

const children = (() => {}) as unknown as import('svelte').Snippet;
const feeds: Feed[] = [
  {
    id: 1,
    title: 'Example news',
    site_url: 'https://example.com',
    category: { id: 10, title: 'News' },
  },
  {
    id: 2,
    title: 'Example art',
    site_url: 'https://example.org',
    category: { id: 20, title: 'Art' },
  },
];
const response = { feeds, entries: [], total: 0 };
const currentUrl = () => __history.entries[__history.index].url;

beforeEach(() => {
  vi.clearAllMocks();
  __history.reset('/istota/feeds/');
  feedsList.set([]);
  selectedFeedId.set(0);
  selectedCategoryId.set(0);
  showStarred.set(false);
  showUnseen.set(false);
  feedsRefreshNonce.set(0);
  vi.mocked(getFeeds).mockResolvedValue(response);
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openReader() {
  render(Layout, { children });
  await screen.findByRole('button', { name: 'Example news' });
  await tick();
}

function navigate(url: string) {
  window.history.replaceState(null, '', url);
  page.url = new URL(window.location.href);
  page.state = {};
}

describe('feed selection history', () => {
  it('pushes a feed and toggles it back to All, with Back and Forward restoration', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: 'Example news' }));
    expect(currentUrl()).toBe('/istota/feeds/?feed=1');
    await fireEvent.click(screen.getByRole('button', { name: 'Example news' }));
    expect(currentUrl()).toBe('/istota/feeds/');
    expect(__history.entries).toHaveLength(3);
    __history.back();
    await waitFor(() => expect(get(selectedFeedId)).toBe(1));
    __history.forward();
    await waitFor(() => expect(get(selectedFeedId)).toBe(0));
    expect(__history.entries).toHaveLength(3);
  });

  it('pushes exclusive category, Starred, Unread and All selections', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: 'News 1' }));
    expect(currentUrl()).toBe('/istota/feeds/?category=10');
    expect(get(selectedCategoryId)).toBe(10);
    await fireEvent.click(screen.getByRole('button', { name: 'Starred', exact: true }));
    expect(currentUrl()).toBe('/istota/feeds/?view=starred');
    expect(get(selectedCategoryId)).toBe(0);
    expect(get(showStarred)).toBe(true);
    await fireEvent.click(screen.getByRole('button', { name: 'Unread', exact: true }));
    expect(currentUrl()).toBe('/istota/feeds/?view=unread');
    expect(get(showStarred)).toBe(false);
    expect(get(showUnseen)).toBe(true);
    await fireEvent.click(screen.getByRole('button', { name: 'Unread', exact: true }));
    expect(__history.entries).toHaveLength(4);
    await fireEvent.click(screen.getByRole('button', { name: 'All', exact: true }));
    expect(currentUrl()).toBe('/istota/feeds/');
    expect(get(showUnseen)).toBe(false);
    expect(__history.entries).toHaveLength(5);
    __history.back();
    await waitFor(() => expect(get(showUnseen)).toBe(true));
    __history.back();
    await waitFor(() => expect(get(showStarred)).toBe(true));
    __history.back();
    await waitFor(() => expect(get(selectedCategoryId)).toBe(10));
    expect(get(showStarred)).toBe(false);
  });

  it('toggles an active category back to All', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: 'News 1' }));
    await fireEvent.click(screen.getByRole('button', { name: 'News 1' }));
    expect(get(selectedCategoryId)).toBe(0);
    expect(currentUrl()).toBe('/istota/feeds/');
    expect(__history.entries).toHaveLength(3);
  });

  it.each(['feed=2', 'category=20', 'view=starred', 'view=unread'])(
    'restores %s after a delayed list load without adding history',
    async (query) => {
      __history.reset(`/istota/feeds/?${query}`);
      let finish!: (value: typeof response) => void;
      vi.mocked(getFeeds).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      render(Layout, { children });
      await waitFor(() => expect(getFeeds).toHaveBeenCalled());
      expect(currentUrl()).toBe(`/istota/feeds/?${query}`);
      finish(response);
      await screen.findByRole('button', { name: 'Example news' });
      await tick();
      expect(get(selectedFeedId)).toBe(query === 'feed=2' ? 2 : 0);
      expect(get(selectedCategoryId)).toBe(query === 'category=20' ? 20 : 0);
      expect(get(showStarred)).toBe(query === 'view=starred');
      expect(get(showUnseen)).toBe(query === 'view=unread');
      expect(currentUrl()).toBe(`/istota/feeds/?${query}`);
      expect(__history.entries).toHaveLength(1);
    },
  );

  it.each(['feed=999', 'feed=-1', 'feed=1.5', 'category=999', 'view=missing'])(
    'replaces missing or invalid %s with All',
    async (query) => {
      __history.reset(`/istota/feeds/?${query}`);
      await openReader();
      expect(currentUrl()).toBe('/istota/feeds/');
      expect(get(selectedFeedId)).toBe(0);
      expect(get(selectedCategoryId)).toBe(0);
      expect(__history.entries).toHaveLength(1);
    },
  );

  it('replaces Back onto a removed feed with the current valid selection', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: 'Example news' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Example art' }));
    feedsList.set([feeds[1]]);
    await tick();
    __history.back();
    await waitFor(() => expect(currentUrl()).toBe('/istota/feeds/?feed=2'));
    expect(get(selectedFeedId)).toBe(2);
    expect(__history.entries).toHaveLength(3);
  });

  it('carries the selection back from settings without rewriting the settings URL', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: 'Example news' }));
    await fireEvent.click(screen.getByTitle('Feed settings'));
    expect(goto).toHaveBeenLastCalledWith('/istota/feeds/settings');
    __history.reset('/istota/feeds/settings/');
    await tick();
    selectedFeedId.set(2);
    await tick();
    expect(currentUrl()).toBe('/istota/feeds/settings/');
    await fireEvent.click(screen.getByTitle('Feed settings'));
    expect(goto).toHaveBeenLastCalledWith('/istota/feeds/?feed=2');
    expect(__history.entries).toHaveLength(1);
  });

  it('restores a new query when the layout survives settings navigation', async () => {
    __history.reset('/istota/feeds/settings/');
    render(Layout, { children });
    await waitFor(() => expect(get(feedsList)).toHaveLength(2));
    navigate('/istota/feeds/?category=20');
    await waitFor(() => expect(get(selectedCategoryId)).toBe(20));
    expect(window.location.search).toBe('?category=20');
  });

  it('validates against refreshed subscriptions when returning from settings', async () => {
    await openReader();
    await fireEvent.click(screen.getByRole('button', { name: 'Example news' }));
    navigate('/istota/feeds/settings/');
    await tick();
    vi.mocked(getFeeds).mockResolvedValue({ ...response, feeds: [feeds[1]] });
    navigate('/istota/feeds/?feed=1');
    await waitFor(() => expect(get(selectedFeedId)).toBe(0));
    expect(window.location.search).toBe('');
  });

  it('ignores an older list response after a settings round trip', async () => {
    let finish!: (value: typeof response) => void;
    vi.mocked(getFeeds).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(Layout, { children });
    await waitFor(() => expect(getFeeds).toHaveBeenCalledTimes(1));
    navigate('/istota/feeds/settings/');
    await tick();
    vi.mocked(getFeeds).mockResolvedValue({ ...response, feeds: [feeds[1]] });
    navigate('/istota/feeds/?feed=2');
    await waitFor(() => expect(get(selectedFeedId)).toBe(2));
    finish(response);
    await tick();
    expect(get(feedsList)).toEqual([feeds[1]]);
    expect(window.location.search).toBe('?feed=2');
  });

  it('preserves the reload query when subscriptions fail to load', async () => {
    __history.reset('/istota/feeds/?feed=2');
    vi.mocked(getFeeds).mockRejectedValue(new TypeError('Failed to fetch'));
    render(Layout, { children });
    await waitFor(() => expect(getFeeds).toHaveBeenCalled());
    await tick();
    expect(currentUrl()).toBe('/istota/feeds/?feed=2');
    expect(__history.entries).toHaveLength(1);
  });

  it('keeps view history usable after the subscription request fails', async () => {
    vi.mocked(getFeeds).mockImplementation(async (params) => {
      if (params?.limit === '1') throw new TypeError('Failed to fetch');
      return response;
    });
    render(Harness, { layout: Layout, component: Page, user: { username: 'alice' } as User });
    const scrollRoot = document.querySelector('.shell-main') as HTMLElement;
    scrollRoot.scrollTo = vi.fn();
    await waitFor(() => expect(getFeeds).toHaveBeenCalledWith({ limit: '1', offset: '0' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Unread', exact: true }));
    await fireEvent.click(screen.getByRole('button', { name: 'Starred', exact: true }));
    vi.mocked(getFeeds).mockClear();
    __history.back();
    await waitFor(() => expect(get(showUnseen)).toBe(true));
    expect(get(showStarred)).toBe(false);
    expect(getFeeds).toHaveBeenCalledWith(expect.objectContaining({ status: 'unread' }));
  });

  it.each(['success', 'error'])(
    'ignores a late initial All %s after restoring a feed in the actual reader',
    async (outcome) => {
      __history.reset('/istota/feeds/?feed=2');
      const entry: FeedEntry = {
        id: 2,
        title: 'Selected article',
        url: 'https://example.org/article',
        content: '<p>Selected feed body</p>',
        images: [],
        duplicate_image_count: 0,
        embed_url: '',
        file_url: '',
        media_url: '',
        media_type: '',
        feed: feeds[1],
        status: 'read',
        starred: false,
        starred_at: '',
        published_at: '',
        created_at: '',
      };
      let finish!: () => void;
      vi.mocked(getFeeds).mockImplementation((params) => {
        if (params?.limit === '1') return Promise.resolve(response);
        if (params?.feed_id === '2')
          return Promise.resolve({ ...response, entries: [entry], total: 1 });
        return new Promise((resolve, reject) => {
          finish = () =>
            outcome === 'error' ? reject(new Error('Old request failed')) : resolve(response);
        });
      });
      render(Harness, { layout: Layout, component: Page, user: { username: 'alice' } as User });
      const scrollRoot = document.querySelector('.shell-main') as HTMLElement;
      scrollRoot.scrollTo = vi.fn();
      await screen.findByText('Selected feed body');
      finish();
      await tick();
      await tick();
      expect(screen.getByText('Selected feed body')).toBeTruthy();
      expect(screen.queryByText('Failed to load feeds')).toBeNull();
      expect(get(selectedFeedId)).toBe(2);
      expect(currentUrl()).toBe('/istota/feeds/?feed=2');
    },
  );

  it('restores the actual reader API filter on Back', async () => {
    render(Harness, { layout: Layout, component: Page, user: { username: 'alice' } as User });
    const scrollRoot = document.querySelector('.shell-main') as HTMLElement;
    scrollRoot.scrollTo = vi.fn();
    await screen.findByRole('button', { name: 'Example news' });
    await fireEvent.click(screen.getByRole('button', { name: 'Example news' }));
    await waitFor(() =>
      expect(getFeeds).toHaveBeenCalledWith(expect.objectContaining({ feed_id: '1', limit: '50' })),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Example art' }));
    await waitFor(() =>
      expect(getFeeds).toHaveBeenCalledWith(expect.objectContaining({ feed_id: '2', limit: '50' })),
    );
    vi.mocked(getFeeds).mockClear();
    __history.back();
    await waitFor(() =>
      expect(getFeeds).toHaveBeenCalledWith(expect.objectContaining({ feed_id: '1', limit: '50' })),
    );
    expect(get(selectedFeedId)).toBe(1);
    expect(__history.entries).toHaveLength(3);
  });
});
