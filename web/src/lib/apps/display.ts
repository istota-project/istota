/**
 * An app's display range against the pane it is given.
 *
 * The three ranges are minot's breakpoints: `phone` works from 320px, `tablet`
 * needs more than 640px and `desktop` 1024px or more. They are measured against
 * the pane the app gets, never the device, so an iPad in split view or a narrow
 * desktop window counts as what it is. A pane too narrow is never a refusal:
 * the list badges the app, the view offers "Open anyway", and an app already
 * open keeps running under an `Alert`.
 */
import type { Narrowest } from './api';

/** The narrowest pane width, in CSS px, each range works in. */
export const RANGE_MIN_WIDTH: Readonly<Record<Narrowest, number>> = {
  phone: 0,
  tablet: 641,
  desktop: 1024,
};

/**
 * Whether a pane `width` px wide fits `narrowest`. A width not measured yet
 * (`null`) fits, so nothing flashes "made for a wider screen" before the first
 * measurement; an unknown range reads as `phone`, the manifest's default.
 */
export function fitsRange(narrowest: Narrowest | null | undefined, width: number | null): boolean {
  if (width === null) return true;
  const min =
    narrowest && Object.hasOwn(RANGE_MIN_WIDTH, narrowest) ? RANGE_MIN_WIDTH[narrowest] : 0;
  return width >= min;
}

/** A range as the "made for …" line reads it. */
export function rangeLabel(narrowest: Narrowest): string {
  return narrowest === 'desktop' ? 'a desktop-width screen' : 'a tablet-width screen or wider';
}

/**
 * Report `el`'s width now and whenever it changes. Without `ResizeObserver`
 * (an old WebView, a test DOM) it reports nothing, and `fitsRange` reads the
 * missing width as fitting. Returns the cleanup.
 */
export function observeWidth(el: HTMLElement, onWidth: (width: number) => void): () => void {
  if (typeof ResizeObserver === 'undefined') return () => {};
  const observer = new ResizeObserver((entries) => {
    const entry = entries[entries.length - 1];
    if (entry) onWidth(Math.round(entry.contentRect.width));
  });
  observer.observe(el);
  return () => observer.disconnect();
}
