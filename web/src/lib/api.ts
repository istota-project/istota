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
	};
}

export interface FeedEntry {
	id: number;
	title: string;
	url: string;
	content: string;
	images: string[];
	feed: { id: number; title: string; site_url: string };
	status: string;
	published_at: string;
	created_at: string;
}

export interface FeedsResponse {
	feeds: { id: number; title: string; site_url: string }[];
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

export { AuthError };
