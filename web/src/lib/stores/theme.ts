import { writable } from 'svelte/store';
import { loadSetting, saveSetting } from '@minot/ui/ui/persisted';

export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'theme';

function normalize(value: unknown): Theme {
  return value === 'light' ? 'light' : 'dark';
}

/** The OS's light or dark setting, for someone who has not chosen a theme. */
function systemTheme(): Theme {
  if (typeof window === 'undefined') return 'dark';
  return window.matchMedia?.('(prefers-color-scheme: light)').matches === true ? 'light' : 'dark';
}

function initialTheme(): Theme {
  const stored = loadSetting<unknown>(STORAGE_KEY, null);
  return stored === 'light' || stored === 'dark' ? stored : systemTheme();
}

/** Current theme. Mirrors the `data-theme` attribute on <html>. */
export const theme = writable<Theme>(initialTheme());

/**
 * Mobile browser chrome color per theme. Mirrors `--surface-card` in app.css
 * and the seed values in the pre-paint script in app.html — keep all three in
 * step.
 */
// design-lint-allow-begin: this IS the token's value, not a use of it — the
// browser chrome meta tag takes a literal and cannot read a CSS variable.
const CHROME_COLOR: Record<Theme, string> = {
  dark: '#1a1a1a',
  light: '#f4f4f5',
};
// design-lint-allow-end

/** Reflect the theme onto <html> so the CSS variable overrides take effect. */
export function applyTheme(value: Theme): void {
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-theme', value);
    const chrome = document.querySelector('meta[name="theme-color"]');
    if (chrome) chrome.setAttribute('content', CHROME_COLOR[value]);
  }
}

/** Set, persist, and apply a specific theme. */
export function setTheme(value: Theme): void {
  const next = normalize(value);
  theme.set(next);
  saveSetting(STORAGE_KEY, next);
  applyTheme(next);
}

/** Flip between light and dark, persisting the result. */
export function toggleTheme(): void {
  let next: Theme = 'dark';
  theme.update((current) => {
    next = current === 'dark' ? 'light' : 'dark';
    return next;
  });
  saveSetting(STORAGE_KEY, next);
  applyTheme(next);
}
