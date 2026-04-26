import { writable } from 'svelte/store';
import { getLocationPlaces, type DiscoveredCluster, type Place } from '$lib/api';

/** Shared places list, loaded once by the location layout. */
export const locationPlaces = writable<Place[]>([]);

/** Callback to fly the active map to a position. Set by each page's map. */
export const mapFlyTo = writable<((lat: number, lon: number, zoom?: number) => void) | undefined>(undefined);

/** Currently selected place ID (for drag-to-reposition on the map). */
export const selectedPlaceId = writable<number | null>(null);

/** Callback when a place is dragged on the map. Set by layout. */
export const onPlaceMove = writable<((placeId: number, lat: number, lon: number) => void) | undefined>(undefined);

/** True when the user is in "+ New place" picking mode. Set by layout, read by route maps. */
export const pickingPlace = writable<boolean>(false);

/**
 * Layout-provided handler invoked when the active map is clicked while
 * `pickingPlace` is true, or when a discovered cluster is clicked.
 */
export const requestNewPlace = writable<
	((args: { lat: number; lon: number; cluster?: DiscoveredCluster }) => void) | undefined
>(undefined);

/**
 * Bumped whenever a place or cluster mutation completes (create/dismiss). Pages
 * subscribed to clusters/dismissed lists watch this to know when to reload.
 */
export const discoverDirty = writable<number>(0);

export function bumpDiscoverDirty(): void {
	discoverDirty.update((n) => n + 1);
}

/** Reload places from the API and update the store. */
export async function reloadPlaces(): Promise<void> {
	const resp = await getLocationPlaces();
	locationPlaces.set(resp.places);
}
