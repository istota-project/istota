// design-lint-allow-file: data-viz. Chart.js reads a plain config object rather
// than the CSS cascade, so it cannot resolve `var(--token)` — a chart has to be
// handed literals. This module is the one place those literals live, so the
// chart chrome tracks the theme the same way the rest of the app does instead of
// being hardcoded dark on each page.

import { get } from 'svelte/store';
import { theme, type Theme } from './stores/theme';

/**
 * Non-data chart colors — everything except the series themselves. Series colors
 * stay on their own pages: they are categorical and chosen to read on both
 * themes, whereas this chrome has to flip.
 */
export interface ChartChrome {
  /** Gridlines behind the plot. */
  grid: string;
  /** Axis tick labels. */
  tick: string;
  /** A neutral series that exists to be read against the background (the cash
   * flow "Net" line), so unlike a categorical series it must flip. */
  neutral: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipTitle: string;
  tooltipBody: string;
}

// Mirrors app.css: `tick` tracks --text-dim/--text-muted, the tooltip tracks
// --surface-card / --border-default / --text-primary / --text-secondary. Kept as
// literals here for the reason in the file header.
const CHROME: Record<Theme, ChartChrome> = {
  dark: {
    grid: 'rgba(255, 255, 255, 0.05)',
    tick: '#666',
    neutral: '#e0e0e0',
    tooltipBg: '#1a1a1a',
    tooltipBorder: '#333',
    tooltipTitle: '#e0e0e0',
    tooltipBody: '#bbb',
  },
  light: {
    grid: 'rgba(0, 0, 0, 0.08)',
    // Not --text-dim's light value (#9a9aa2): a tick label that pale is
    // unreadable on white, so this tracks --text-muted instead.
    tick: '#6b6b72',
    neutral: '#3f3f46',
    tooltipBg: '#ffffff',
    tooltipBorder: '#d4d4d8',
    tooltipTitle: '#1a1a1a',
    tooltipBody: '#3f3f46',
  },
};

/**
 * Chart chrome for a theme, defaulting to the current one.
 *
 * Read this inside the render function rather than at module scope, so a chart
 * rebuilt after a theme change picks up the new values.
 */
export function chartChrome(value: Theme = get(theme)): ChartChrome {
  return CHROME[value];
}
