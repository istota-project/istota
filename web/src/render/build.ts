// The pure half of `vite.render.config.ts`: which entries there are, and the
// document shell a bundle is inlined into. Node-only; no bundle imports it.

import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

/** The renderer names: every directory under `src/render/` holding a `main.ts`. */
export function renderEntries(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && existsSync(join(dir, entry.name, 'main.ts')))
    .map((entry) => entry.name)
    .sort();
}

// A literal `</script` or `<!--` inside an inline script would end it or
// switch the HTML parser's state; both are escaped where they can only be
// JavaScript string or regex text, which reads the same either way.
function inlineScript(code: string): string {
  return code.replace(/<\/script/gi, '<\\/script').replace(/<!--/g, '<\\!--');
}

function inlineStyle(css: string): string {
  return css.replace(/<\/style/gi, '<\\/style');
}

/** The document shell: the bundle's CSS and JS inline, nothing fetched. */
export function renderDocument(name: string, js: string, css: string): string {
  // Vite's preload placeholder, left in when this runs before Vite replaces
  // it: every inlined dynamic import would then reject in the browser.
  if (js.includes('__VITE_PRELOAD__')) {
    throw new Error(`render bundle ${name} still holds Vite's __VITE_PRELOAD__ placeholder`);
  }
  return [
    '<!doctype html>',
    '<html lang="en" data-theme="light">',
    '<head>',
    '<meta charset="utf-8">',
    `<title>istota render: ${name}</title>`,
    `<style>${inlineStyle(css)}</style>`,
    '</head>',
    '<body>',
    '<div data-render-frame></div>',
    `<script type="module">${inlineScript(js)}</script>`,
    '</body>',
    '</html>',
    '',
  ].join('\n');
}
