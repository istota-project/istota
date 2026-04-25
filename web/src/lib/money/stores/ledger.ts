import { writable } from 'svelte/store';

/** Selected ledger name, or empty string for default (first configured). */
export const selectedLedger = writable('');

/** Available ledger names, populated on app load. */
export const availableLedgers = writable<string[]>([]);
