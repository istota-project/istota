import { writable } from 'svelte/store';
import type { Feed } from '$lib/api';
import { loadSetting, saveSetting } from '$lib/stores/persisted';

/** Shared feeds list, loaded once by the feeds layout. */
export const feedsList = writable<Feed[]>([]);

/** Currently selected feed ID for sidebar filtering (0 = all). */
export const selectedFeedId = writable<number>(0);

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
