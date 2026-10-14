import { derived, writable } from 'svelte/store';

// How many panes are loading right now. A count rather than a flag because the
// root layout, a module layout and the page under it can all be loading at once
// on a cold load, and the bar must stay up until the last of them is done.
const holds = writable(0);

export const pageLoading = derived(holds, (n) => n > 0);

/** Mark a pane as loading; call the returned function once it is not. */
export function holdPageLoading(): () => void {
  holds.update((n) => n + 1);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holds.update((n) => n - 1);
  };
}
