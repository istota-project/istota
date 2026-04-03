import { writable } from 'svelte/store';
import type { Place } from '$lib/api';

/** Shared places list, loaded once by the location layout. */
export const locationPlaces = writable<Place[]>([]);

/** Callback to fly the active map to a position. Set by each page's map. */
export const mapFlyTo = writable<((lat: number, lon: number, zoom?: number) => void) | null>(null);
