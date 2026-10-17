import { describe, it, expect } from 'vitest';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { renderDocument, renderEntries } from './build';

const HERE = dirname(fileURLToPath(import.meta.url));

describe('the render build', () => {
  it('finds every entry directory with a main.ts, and nothing else', () => {
    expect(renderEntries(HERE)).toContain('share-map');
    for (const name of renderEntries(HERE)) expect(name).toMatch(/^[a-z0-9-]{1,40}$/);
  });

  it('writes one document with the CSS and JS inline and the render frame in it', () => {
    const html = renderDocument('share-map', 'window.x = 1;', '.a{color:red}');
    expect(html).toContain('<style>.a{color:red}</style>');
    expect(html).toContain('<script type="module">window.x = 1;</script>');
    expect(html).toContain('<div data-render-frame></div>');
    expect(html).not.toMatch(/<script[^>]+src=/);
    expect(html).not.toMatch(/<link/);
  });

  it("refuses a bundle still holding Vite's preload placeholder", () => {
    const js = 'await __vitePreload(() => Promise.resolve().then(() => m), __VITE_PRELOAD__);';
    expect(() => renderDocument('share-map', js, '')).toThrow(/__VITE_PRELOAD__/);
  });

  it('keeps a script or style literal from ending its element early', () => {
    const html = renderDocument('x', 'const s = "</script><!--";', 'a::after{content:"</style>"}');
    expect(html.match(/<\/script>/g)).toHaveLength(1);
    expect(html.match(/<\/style>/g)).toHaveLength(1);
    expect(html).not.toContain('<!--');
  });
});
