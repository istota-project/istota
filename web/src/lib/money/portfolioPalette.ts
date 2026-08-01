// design-lint-allow-file: data-viz. Chart.js reads a plain config object, not
// the CSS cascade, so series colors have to be literals. This module is the one
// place the portfolio charts' categorical palette lives — the dataviz skill's
// validated 8-slot set, stepped per theme (dark slots re-stepped for the dark
// card surface, not a hue flip). Assign slots in fixed order by label, never
// cycled per chart, so a label keeps its color across every chart and filter.

import { get } from 'svelte/store';
import { theme, type Theme } from '../stores/theme';

const SLOTS: Record<Theme, string[]> = {
  light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
  dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
};

/** Neutral for the "Other" fold and for Unclassified slices. */
const NEUTRAL: Record<Theme, string> = { light: '#8a8a92', dark: '#6f6f76' };

export const SERIES_CAP = 8;

/**
 * Map a fixed, ordered label list to series colors. Labels beyond the 8
 * validated slots (and "Unclassified") get the neutral — fold long tails into
 * "Other" before calling when possible.
 */
export function seriesColors(labels: string[], mode?: Theme): Map<string, string> {
  const t = mode ?? get(theme);
  const slots = SLOTS[t];
  const out = new Map<string, string>();
  let slot = 0;
  for (const label of labels) {
    if (label === 'Unclassified' || label === 'Other' || slot >= slots.length) {
      out.set(label, NEUTRAL[t]);
    } else {
      out.set(label, slots[slot]);
      slot += 1;
    }
  }
  return out;
}
