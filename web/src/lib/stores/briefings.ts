import { writable } from 'svelte/store';

// Reader selection shared between the briefings layout (archive sidebar) and
// the reader page (main pane), mirroring the feeds store split.
export const selectedBriefingId = writable<number | null>(null);
export const briefingFilterName = writable<string>('');

// null = archive still loading; 0 = loaded and empty. Lets the reader page
// distinguish "loading" from "nothing to show" without owning the list fetch.
export const briefingArchiveCount = writable<number | null>(null);

// Why the archive is not on screen, when the reason is a failed fetch rather
// than an empty account. The count alone cannot carry this: a failed list fetch
// leaves zero items, which is byte-identical to a genuinely empty archive, and
// the reader rendered "No briefings yet — once a scheduled briefing runs it
// will appear here" at a user who was merely offline and had briefings
// configured. It lives beside the count rather than being folded into it (a
// sentinel like -1) because the two answer different questions and the reader
// asks both: how many, and did asking work.
//
// The reader page cannot derive it either. It fetches only the *selected*
// briefing, and a failed list fetch leaves nothing selected, so its own catch
// is never reached — which is what made the layout's "the reader page surfaces
// its own load errors" a false premise for the one case that mattered.
export const briefingArchiveError = writable<string | null>(null);

// Bumped by the settings page after a schedule change so the reader's archive
// sidebar can refresh without a full navigation.
export const briefingsRefreshNonce = writable(0);
