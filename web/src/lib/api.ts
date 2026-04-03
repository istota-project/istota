import { base } from '$app/paths';

class AuthError extends Error {
	constructor() {
		super('Not authenticated');
		this.name = 'AuthError';
	}
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
	const resp = await fetch(`${base}/api${path}`, {
		...init,
		credentials: 'same-origin',
	});
	if (resp.status === 401) throw new AuthError();
	if (!resp.ok) throw new Error(`API error: ${resp.status}`);
	return resp.json();
}

export interface User {
	username: string;
	display_name: string;
	features: {
		feeds: boolean;
		location: boolean;
	};
}

export interface FeedCategory {
	id: number;
	title: string;
}

export interface Feed {
	id: number;
	title: string;
	site_url: string;
	category: FeedCategory;
}

export interface FeedEntry {
	id: number;
	title: string;
	url: string;
	content: string;
	images: string[];
	feed: Feed;
	status: string;
	published_at: string;
	created_at: string;
}

export interface FeedsResponse {
	feeds: Feed[];
	entries: FeedEntry[];
	total: number;
}

export async function getMe(): Promise<User> {
	return apiFetch<User>('/me');
}

export async function getFeeds(params?: Record<string, string>): Promise<FeedsResponse> {
	const qs = params ? '?' + new URLSearchParams(params).toString() : '';
	return apiFetch<FeedsResponse>(`/feeds${qs}`);
}

export async function updateEntryStatus(id: number, status: string): Promise<void> {
	await apiFetch(`/feeds/entries/${id}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ status }),
	});
}

export async function updateEntriesStatus(ids: number[], status: string): Promise<void> {
	await apiFetch('/feeds/entries/batch', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ entry_ids: ids, status }),
	});
}

// Location types

export interface LocationPing {
	timestamp: string;
	lat: number;
	lon: number;
	accuracy: number;
	place: string | null;
	speed: number | null;
	battery: number | null;
}

export interface CurrentLocation {
	last_ping: LocationPing | null;
	current_visit: {
		place_name: string;
		entered_at: string;
		duration_minutes: number | null;
		ping_count: number;
	} | null;
}

export interface DaySummaryStop {
	location: string;
	location_source: string | null;
	arrived: string;
	departed: string;
	ping_count: number;
	lat: number;
	lon: number;
}

export interface DaySummary {
	date: string;
	timezone: string;
	ping_count: number;
	transit_pings: number;
	stops: DaySummaryStop[];
}

export interface PingsResponse {
	pings: LocationPing[];
	count: number;
}

export interface Place {
	name: string;
	lat: number;
	lon: number;
	radius_meters: number;
	category: string;
}

export interface PlacesResponse {
	places: Place[];
}

// Location API

export async function getLocationCurrent(): Promise<CurrentLocation> {
	return apiFetch<CurrentLocation>('/location/current');
}

export async function getLocationPings(params: Record<string, string>): Promise<PingsResponse> {
	const qs = '?' + new URLSearchParams(params).toString();
	return apiFetch<PingsResponse>(`/location/pings${qs}`);
}

export async function getDaySummary(date?: string): Promise<DaySummary> {
	const qs = date ? `?date=${date}` : '';
	return apiFetch<DaySummary>(`/location/day-summary${qs}`);
}

export async function getLocationPlaces(): Promise<PlacesResponse> {
	return apiFetch<PlacesResponse>('/location/places');
}

export { AuthError };
