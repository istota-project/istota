import { writable } from 'svelte/store';
import { loadSetting, saveSetting } from './persisted';

export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'theme';

function normalize(value: unknown): Theme {
	return value === 'light' ? 'light' : 'dark';
}

function initialTheme(): Theme {
	return normalize(loadSetting<Theme>(STORAGE_KEY, 'dark'));
}

/** Current theme. Mirrors the `data-theme` attribute on <html>. */
export const theme = writable<Theme>(initialTheme());

/** Reflect the theme onto <html> so the CSS variable overrides take effect. */
export function applyTheme(value: Theme): void {
	if (typeof document !== 'undefined') {
		document.documentElement.setAttribute('data-theme', value);
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
