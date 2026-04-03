import { writable } from 'svelte/store';
import { getLocationPlaces, type Place } from '$lib/api';

/** Shared places list, loaded once by the location layout. */
export const locationPlaces = writable<Place[]>([]);

/** Callback to fly the active map to a position. Set by each page's map. */
export const mapFlyTo = writable<((lat: number, lon: number, zoom?: number) => void) | null>(null);

/** Currently selected place ID (for drag-to-reposition on the map). */
export const selectedPlaceId = writable<number | null>(null);

/** Callback when a place is dragged on the map. Set by layout. */
export const onPlaceMove = writable<((placeId: number, lat: number, lon: number) => void) | null>(null);

/** Reload places from the API and update the store. */
export async function reloadPlaces(): Promise<void> {
	const resp = await getLocationPlaces();
	locationPlaces.set(resp.places);
}
