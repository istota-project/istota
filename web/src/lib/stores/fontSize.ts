import { writable } from 'svelte/store';
import { loadSetting, saveSetting } from './persisted';

export type FontSize = 'small' | 'medium' | 'large';

const STORAGE_KEY = 'fontSize';

/**
 * Selectable sizes, smallest first. `medium` is the default; `small` is the
 * historical unscaled size, kept as an explicit opt-in for a denser layout.
 */
export const FONT_SIZES: readonly FontSize[] = ['small', 'medium', 'large'];

/** Anything unrecognised (absent, corrupt, a retired value) resolves to the default. */
function normalize(value: unknown): FontSize {
  return value === 'small' || value === 'large' ? value : 'medium';
}

function initialFontSize(): FontSize {
  return normalize(loadSetting<FontSize>(STORAGE_KEY, 'medium'));
}

/** Current font size. Mirrors the `data-font-size` attribute on <html>. */
export const fontSize = writable<FontSize>(initialFontSize());

/** Reflect the size onto <html> so the root font-size override takes effect. */
export function applyFontSize(value: FontSize): void {
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-font-size', normalize(value));
  }
}

/** Set, persist, and apply a specific font size. */
export function setFontSize(value: FontSize): void {
  const next = normalize(value);
  fontSize.set(next);
  saveSetting(STORAGE_KEY, next);
  applyFontSize(next);
}
