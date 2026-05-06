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
	is_admin: boolean;
	features: {
		feeds: boolean;
		location: boolean;
		money: boolean;
		google_workspace: boolean;
		google_workspace_enabled: boolean;
		admin: boolean;
	};
}

export interface AdminStatsUser {
	username: string;
	display_name: string;
	is_admin: boolean;
	tasks_total: number;
	tasks_last_24h: number;
	tasks_avg_per_day: number;
	last_active: string | null;
}

export interface AdminStatsJob {
	id: number;
	user_id: string;
	name: string;
	cron: string;
	enabled: boolean;
	last_run_at: string | null;
	last_success_at: string | null;
	consecutive_failures: number;
	last_error: string | null;
}

export interface AdminStats {
	system: {
		version: string;
		uptime_seconds: number;
		db_size_bytes: number;
		python_version: string;
		last_scheduler_run: string | null;
		scheduler_healthy: boolean;
	};
	users: AdminStatsUser[];
	scheduler: {
		jobs_total: number;
		jobs_active: number;
		jobs_paused: number;
		jobs: AdminStatsJob[];
		last_errors: { job_name: string; error: string; timestamp: string | null }[];
	};
	modules: Record<string, Record<string, unknown>>;
	tasks: {
		total: number;
		last_24h: number;
		avg_per_day_30d: number;
		by_source: Record<string, number>;
		avg_duration_seconds: number;
		error_rate_24h: number;
	};
	storage: {
		db_size_bytes: number;
		backups_count: number;
		last_backup: string | null;
		nextcloud_mount_healthy: boolean;
	};
	error?: string;
}

export async function getAdminStats(): Promise<AdminStats> {
	return apiFetch<AdminStats>('/admin/stats');
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
	starred: boolean;
	starred_at: string;
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

export async function updateEntryStarred(id: number, starred: boolean): Promise<void> {
	await apiFetch(`/feeds/entries/${id}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ starred }),
	});
}

export type MarkAsReadScope = 'all' | 'feed' | 'category';

export async function markAsRead(
	scope: MarkAsReadScope,
	opts?: { id?: number; before_id?: number },
): Promise<{ status: string; updated: number }> {
	const body: Record<string, unknown> = { scope };
	if (opts?.id != null) body.id = opts.id;
	if (opts?.before_id != null) body.before_id = opts.before_id;
	return apiFetch('/feeds/mark-as-read', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
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

export async function refreshFeeds(): Promise<{ status: string; feeds_queued: number }> {
	return apiFetch('/feeds/refresh', { method: 'POST' });
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

// ---- Settings (Phase 5) ----

export interface SettingsField {
	key: string;
	label: string;
	type: 'text' | 'email' | 'password' | 'url';
}

export interface ServiceCard {
	service: string;
	label: string;
	status: 'configured' | 'partial' | 'missing' | 'unavailable';
	fields: SettingsField[];
	configured_keys: string[];
	last_updated: string | null;
	used_by?: string[];
	oauth?: boolean;
	connected?: boolean;  // google_workspace OAuth state
	enabled?: boolean;    // google_workspace module flag
}

export interface ServicesResponse {
	services: ServiceCard[];
}

export async function getSettingsServices(): Promise<ServicesResponse> {
	return apiFetch<ServicesResponse>('/settings/services');
}

// --- modules + per-module services ---

export interface ModulesResponse {
	modules: string[];
	disabled: string[];
	enabled_for_user: Record<string, boolean>;
}

export async function getModules(): Promise<ModulesResponse> {
	return apiFetch<ModulesResponse>('/settings/modules');
}

export interface ModuleServicesResponse {
	module: string;
	module_enabled: boolean;
	services: ServiceCard[];
}

export async function getModuleServices(
	module: string,
): Promise<ModuleServicesResponse> {
	return apiFetch<ModuleServicesResponse>(`/settings/module-services/${module}`);
}

export interface LocationSettingsInfo {
	webhook_url: string;
	module_enabled: boolean;
	place_detection: {
		accuracy_threshold_m: number;
		visit_exit_minutes: number;
	};
}

export async function getLocationSettingsInfo(): Promise<LocationSettingsInfo> {
	return apiFetch<LocationSettingsInfo>('/location/settings-info');
}

export async function setSecret(
	service: string,
	key: string,
	value: string,
): Promise<{ ok: boolean; configured: boolean }> {
	return apiFetch(`/settings/secrets/${service}/${key}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ value }),
	});
}

export async function deleteSecret(
	service: string,
	key: string,
): Promise<{ ok: boolean; deleted: boolean }> {
	return apiFetch(`/settings/secrets/${service}/${key}`, {
		method: 'DELETE',
	});
}

// --- Phase 6: profile + resources ---

export interface UserProfile {
	user_id: string;
	display_name: string;
	timezone: string;
	email_addresses: string[];
	trusted_email_senders: string[];
	ntfy_topic: string;
	log_channel: string;
	alerts_channel: string;
	disabled_skills: string[];
	disabled_modules: string[];
	max_foreground_workers: number;
	max_background_workers: number;
	site_enabled: boolean;
}

export async function getProfile(): Promise<{ profile: UserProfile | null }> {
	return apiFetch<{ profile: UserProfile | null }>('/settings/profile');
}

export async function updateProfile(
	patch: Partial<UserProfile>,
): Promise<{ ok: boolean; fields: string[] }> {
	return apiFetch('/settings/profile', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(patch),
	});
}

export interface ResourceTypeSchema {
	type: string;
	label: string;
	needs_path: boolean;
	permissions: string[];
}

export interface UserResourceRow {
	managed: 'config' | 'db';
	id?: number;
	type: string;
	name: string;
	path: string;
	permissions: string;
	extras?: Record<string, unknown>;
}

export async function getResources(): Promise<{
	types: ResourceTypeSchema[];
	resources: UserResourceRow[];
}> {
	return apiFetch('/settings/resources');
}

export async function addResource(payload: {
	type: string;
	path?: string;
	name?: string;
	permissions?: string;
	extras?: Record<string, unknown>;
}): Promise<{ ok: boolean; id: number }> {
	return apiFetch('/settings/resources', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload),
	});
}

export async function deleteResource(
	id: number,
): Promise<{ ok: boolean; deleted: boolean }> {
	return apiFetch(`/settings/resources/${id}`, { method: 'DELETE' });
}

// --- Phase 7b: briefings ---

export interface UserBriefingRow {
	managed: 'config' | 'db';
	id?: number;
	name: string;
	cron: string;
	conversation_token: string;
	output: 'talk' | 'email' | 'both';
	components: Record<string, unknown>;
	enabled: boolean;
}

export interface BriefingRoomOption {
	token: string;
	name: string;
}

export async function getBriefings(): Promise<{
	briefings: UserBriefingRow[];
	rooms: BriefingRoomOption[];
	outputs: string[];
}> {
	return apiFetch('/settings/briefings');
}

export async function upsertBriefing(payload: {
	name: string;
	cron: string;
	conversation_token?: string;
	output?: 'talk' | 'email' | 'both';
	components?: Record<string, unknown>;
	enabled?: boolean;
}): Promise<{ ok: boolean; id: number; state: 'created' | 'updated' | 'noop' }> {
	return apiFetch('/settings/briefings', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload),
	});
}

export async function deleteBriefing(
	id: number,
): Promise<{ ok: boolean; deleted: boolean }> {
	return apiFetch(`/settings/briefings/${id}`, { method: 'DELETE' });
}

export { AuthError };
