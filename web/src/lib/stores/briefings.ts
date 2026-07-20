import { writable } from 'svelte/store';

// Reader selection shared between the briefings layout (archive sidebar) and
// the reader page (main pane), mirroring the feeds store split.
export const selectedBriefingId = writable<number | null>(null);
export const briefingFilterName = writable<string>('');

// null = archive still loading; 0 = loaded and empty. Lets the reader page
// distinguish "loading" from "nothing to show" without owning the list fetch.
export const briefingArchiveCount = writable<number | null>(null);

// Bumped by the settings page after a schedule change so the reader's archive
// sidebar can refresh without a full navigation.
export const briefingsRefreshNonce = writable(0);
