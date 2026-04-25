import { writable } from 'svelte/store';

/** Client-side account name filter text. */
export const accountFilter = writable('');
