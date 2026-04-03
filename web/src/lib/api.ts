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
	activity_type: string | null;
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
	id: number;
	name: string;
	lat: number;
	lon: number;
	radius_meters: number;
	category: string;
}

export interface PlacesResponse {
	places: Place[];
}

export interface PlaceStats {
	place_id: number;
	total_visits: number;
	first_visit: string | null;
	last_visit: string | null;
	avg_duration_min: number | null;
	total_duration_min: number | null;
	longest_visit_min: number | null;
}

export interface DiscoveredCluster {
	lat: number;
	lon: number;
	total_pings: number;
	first_seen: string;
	last_seen: string;
}

export interface DiscoverResponse {
	clusters: DiscoveredCluster[];
}

export interface Trip {
	start_time: string;
	end_time: string;
	start_lat: number;
	start_lon: number;
	end_lat: number;
	end_lon: number;
	distance_m: number;
	ping_count: number;
	activity_type: string;
	max_speed: number | null;
}

export interface TripsResponse {
	date: string;
	trips: Trip[];
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

export async function createPlace(data: { name: string; lat: number; lon: number; radius_meters?: number; category?: string }): Promise<Place> {
	return apiFetch<Place>('/location/places', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(data),
	});
}

export async function updatePlace(id: number, data: Partial<Pick<Place, 'name' | 'lat' | 'lon' | 'radius_meters' | 'category'>>): Promise<Place> {
	return apiFetch<Place>(`/location/places/${id}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(data),
	});
}

export async function deletePlace(id: number): Promise<void> {
	await apiFetch(`/location/places/${id}`, { method: 'DELETE' });
}

export async function getPlaceStats(placeId: number): Promise<PlaceStats> {
	return apiFetch<PlaceStats>(`/location/places/${placeId}/stats`);
}

export async function discoverPlaces(minPings?: number): Promise<DiscoverResponse> {
	const qs = minPings ? `?min_pings=${minPings}` : '';
	return apiFetch<DiscoverResponse>(`/location/discover-places${qs}`);
}

export async function getTrips(date?: string): Promise<TripsResponse> {
	const qs = date ? `?date=${date}` : '';
	return apiFetch<TripsResponse>(`/location/trips${qs}`);
}

export { AuthError };
