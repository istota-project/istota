import { writable } from 'svelte/store';
import { getFeedCounts, type Feed, type FeedCounts } from '$lib/api';
import { loadSetting, saveSetting } from '@minot/ui/ui/persisted';
import { latest } from '@minot/ui/ui/latest';

/** Shared feeds list, loaded once by the feeds layout. */
export const feedsList = writable<Feed[]>([]);

/** Currently selected feed ID for sidebar filtering (0 = all). */
export const selectedFeedId = writable<number>(0);

/** Currently selected category ID for sidebar filtering (0 = none).
 *  Mutually exclusive with selectedFeedId — selecting one clears the other. */
export const selectedCategoryId = writable<number>(0);

/** When true, the reader is filtered to only starred entries. */
export const showStarred = writable(false);

/** Bumped to ask the entries page to reload — used after bulk mutations. */
export const feedsRefreshNonce = writable(0);

/** Totals behind the sidebar's Unread and Starred views. */
export const feedCounts = writable<FeedCounts>({ unread: 0, starred: 0, unread_by_feed: {} });

const countLoads = latest();

/** Re-read the totals. Call after a write the server has confirmed; a
 *  response overtaken by a later request is dropped. */
export async function refreshFeedCounts(): Promise<void> {
  const current = countLoads.next();
  try {
    const next = await getFeedCounts();
    if (!current()) return;
    if (!next || !Number.isSafeInteger(next.unread) || !Number.isSafeInteger(next.starred)) return;
    const byFeed: Record<string, number> = {};
    for (const [id, n] of Object.entries(next.unread_by_feed ?? {})) {
      if (Number.isSafeInteger(n) && n > 0) byFeed[id] = n;
    }
    feedCounts.set({ unread: next.unread, starred: next.starred, unread_by_feed: byFeed });
  } catch {
    // Cosmetic: the badges keep their last value.
  }
}

/** Filter/view state shared between layout (chips) and page (filtering). */
function persistedWritable<T>(key: string, fallback: T) {
  const store = writable<T>(loadSetting(key, fallback));
  store.subscribe((v) => saveSetting(key, v));
  return store;
}

export const showImages = persistedWritable('feeds.showImages', true);
export const showText = persistedWritable('feeds.showText', true);
export const showUnseen = writable(false); // not persisted
export const sortBy = persistedWritable<'published' | 'added'>('feeds.sortBy', 'published');
export const viewMode = persistedWritable<'grid' | 'list'>('feeds.viewMode', 'grid');

/** Seconds a card must be visible before marking as read. */
export const markReadDelay = persistedWritable('feeds.markReadDelay', 1.5);
