/**
 * iOS Safari auto-zooms when a focused text control renders below 16px. A
 * control that keeps a smaller size on purpose, rather than taking the 16px
 * floor in primitives.css, pins the viewport's maximum-scale while it has focus
 * and restores it on blur, so pinch-to-zoom keeps working everywhere else.
 *
 * Both strings must keep viewport-fit=cover (app.html sets it): dropping it on
 * focus would re-letterbox the page mid-interaction and jump the layout.
 */
export const VIEWPORT_DEFAULT = 'width=device-width, initial-scale=1, viewport-fit=cover';
export const VIEWPORT_NO_ZOOM =
  'width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover';

function setViewport(content: string) {
  const meta = document.querySelector('meta[name="viewport"]');
  if (meta) meta.setAttribute('content', content);
}

export function pinViewportZoom() {
  setViewport(VIEWPORT_NO_ZOOM);
}

export function releaseViewportZoom() {
  setViewport(VIEWPORT_DEFAULT);
}
