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
		money: boolean;
		google_workspace: boolean;
		google_workspace_enabled: boolean;
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

// Feeds settings types

export interface FeedsConfigCategory {
	slug: string;
	title?: string;
}

export interface FeedsConfigFeed {
	url: string;
	title?: string;
	category?: string;
	poll_interval_minutes?: number;
}

export interface FeedsConfigSettings {
	default_poll_interval_minutes?: number;
}

export interface FeedsConfigPayload {
	settings: FeedsConfigSettings;
	categories: FeedsConfigCategory[];
	feeds: FeedsConfigFeed[];
}

export interface FeedsDiagnostics {
	total_feeds: number;
	total_entries: number;
	unread_entries: number;
	error_feeds: number;
	last_poll_at: string | null;
}

export interface FeedsFeedState {
	url: string;
	last_fetched_at: string | null;
	last_error: string | null;
	error_count: number;
}

export interface FeedsConfigResponse {
	config: FeedsConfigPayload;
	diagnostics: FeedsDiagnostics;
	feed_state: FeedsFeedState[];
}

export interface FeedsImportResult {
	status: string;
	feeds_added: number;
	feeds_updated: number;
	categories_added: number;
	rewritten_bridger_urls: number;
}

export async function getFeedsConfig(): Promise<FeedsConfigResponse> {
	return apiFetch<FeedsConfigResponse>('/feeds/config');
}

export async function putFeedsConfig(config: FeedsConfigPayload): Promise<{ status: string; sync: { categories_added: number; feeds_added: number; feeds_updated: number } }> {
	return apiFetch('/feeds/config', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ config }),
	});
}

export async function importOpml(file: File): Promise<FeedsImportResult> {
	const fd = new FormData();
	fd.append('file', file);
	const resp = await fetch(`${base}/api/feeds/import-opml`, {
		method: 'POST',
		credentials: 'same-origin',
		body: fd,
	});
	if (resp.status === 401) throw new AuthError();
	if (!resp.ok) {
		let msg = `Import failed: ${resp.status}`;
		try {
			const body = await resp.json();
			if (body?.error) msg = body.error;
		} catch {
			// ignore
		}
		throw new Error(msg);
	}
	return resp.json();
}

export function exportOpmlUrl(): string {
	return `${base}/api/feeds/export-opml`;
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
	notes?: string | null;
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
	radius_meters?: number;
}

export interface DiscoverResponse {
	clusters: DiscoveredCluster[];
}

export interface DismissedCluster {
	id: number;
	lat: number;
	lon: number;
	radius_meters: number;
	dismissed_at: string;
}

export interface DismissedClustersResponse {
	dismissed: DismissedCluster[];
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

function browserTz(): string {
	try {
		return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
	} catch {
		return '';
	}
}

function withBrowserTz(params: Record<string, string>): URLSearchParams {
	const qs = new URLSearchParams(params);
	const tz = browserTz();
	if (tz && !qs.has('tz')) qs.set('tz', tz);
	return qs;
}

export async function getLocationCurrent(): Promise<CurrentLocation> {
	return apiFetch<CurrentLocation>('/location/current');
}

export async function getLocationPings(params: Record<string, string>): Promise<PingsResponse> {
	const qs = withBrowserTz(params).toString();
	return apiFetch<PingsResponse>(`/location/pings?${qs}`);
}

export async function getDaySummary(date?: string): Promise<DaySummary> {
	const params: Record<string, string> = {};
	if (date) params.date = date;
	const qs = withBrowserTz(params).toString();
	return apiFetch<DaySummary>(`/location/day-summary${qs ? '?' + qs : ''}`);
}

export async function getLocationPlaces(): Promise<PlacesResponse> {
	return apiFetch<PlacesResponse>('/location/places');
}

export async function createPlace(data: { name: string; lat: number; lon: number; radius_meters?: number; category?: string; notes?: string | null }): Promise<Place> {
	return apiFetch<Place>('/location/places', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(data),
	});
}

export async function updatePlace(id: number, data: Partial<Pick<Place, 'name' | 'lat' | 'lon' | 'radius_meters' | 'category' | 'notes'>>): Promise<Place> {
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

export async function listDismissedClusters(): Promise<DismissedClustersResponse> {
	return apiFetch<DismissedClustersResponse>('/location/dismissed-clusters');
}

export async function dismissCluster(data: { lat: number; lon: number; radius_meters: number }): Promise<DismissedCluster> {
	return apiFetch<DismissedCluster>('/location/dismissed-clusters', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(data),
	});
}

export async function restoreDismissedCluster(id: number): Promise<void> {
	await apiFetch(`/location/dismissed-clusters/${id}`, { method: 'DELETE' });
}

export async function getTrips(date?: string): Promise<TripsResponse> {
	const params: Record<string, string> = {};
	if (date) params.date = date;
	const qs = withBrowserTz(params).toString();
	return apiFetch<TripsResponse>(`/location/trips${qs ? '?' + qs : ''}`);
}

export async function disconnectGoogle(): Promise<void> {
	await apiFetch('/google/disconnect', { method: 'DELETE' });
}

export { AuthError };
