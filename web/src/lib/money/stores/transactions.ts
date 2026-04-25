import { writable } from 'svelte/store';

/** Full account name selected in the sidebar, or empty string for all. */
export const selectedAccount = writable('');

/** Year filter, or 0 for all years. */
export const selectedYear = writable(new Date().getFullYear());

/** Free-text filter for payee/narration, or #tag for tag filter. */
export const filterText = writable('');
