import { writable } from 'svelte/store';

export interface ServiceDetail {
	id: string;
	name: string;
	description: string;
	status: 'active' | 'loading' | 'error';
	detail: unknown;
}

export const selectedService = writable<ServiceDetail | null>(null);
