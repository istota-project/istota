/**
 * Theme preference store.
 *
 * Beyond the `data-theme` attribute, `applyTheme` keeps the `theme-color` meta
 * in step so the mobile browser chrome follows the in-app toggle. That colour
 * is stated in three places — this module, the pre-paint script in app.html,
 * and `--surface-base` in app.css — so the drift between them is tested too.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

// localStorage is stubbed once for every test file in vitest-setup.ts.

async function load() {
  vi.resetModules();
  return import('./theme');
}

/** The `theme-color` meta the real document carries (added in app.html). */
function addChromeMeta(): HTMLMetaElement {
  const meta = document.createElement('meta');
  meta.setAttribute('name', 'theme-color');
  meta.setAttribute('content', '#111111');
  document.head.appendChild(meta);
  return meta;
}

function chromeColor(): string | null {
  return document.querySelector('meta[name="theme-color"]')?.getAttribute('content') ?? null;
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  document.head.querySelectorAll('meta[name="theme-color"]').forEach((el) => el.remove());
});

afterEach(() => {
  localStorage.clear();
});

describe('theme store', () => {
  it('defaults to dark when nothing is persisted', async () => {
    const { theme } = await load();
    expect(get(theme)).toBe('dark');
  });

  it('seeds from the persisted value', async () => {
    localStorage.setItem('theme', JSON.stringify('light'));
    const { theme } = await load();
    expect(get(theme)).toBe('light');
  });

  it('applyTheme reflects onto <html>', async () => {
    const { applyTheme } = await load();

    applyTheme('light');

    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('applyTheme updates the theme-color meta', async () => {
    addChromeMeta();
    const { applyTheme } = await load();

    applyTheme('light');
    expect(chromeColor()).toBe('#ffffff');

    applyTheme('dark');
    expect(chromeColor()).toBe('#111111');
  });

  // The meta only exists in app.html, which no test renders — applying a theme
  // must not throw when the document happens not to carry one.
  it('applyTheme tolerates a missing theme-color meta', async () => {
    const { applyTheme } = await load();

    expect(() => applyTheme('light')).not.toThrow();
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('setTheme persists and updates both the attribute and the meta', async () => {
    addChromeMeta();
    const { theme, setTheme } = await load();

    setTheme('light');

    expect(get(theme)).toBe('light');
    expect(localStorage.getItem('theme')).toBe(JSON.stringify('light'));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(chromeColor()).toBe('#ffffff');
  });

  it('toggleTheme flips the theme and the meta together', async () => {
    addChromeMeta();
    const { theme, toggleTheme } = await load();

    toggleTheme();
    expect(get(theme)).toBe('light');
    expect(chromeColor()).toBe('#ffffff');

    toggleTheme();
    expect(get(theme)).toBe('dark');
    expect(chromeColor()).toBe('#111111');
  });
});

describe('theme-color drift', () => {
  const root = resolve(__dirname, '../..');
  const css = readFileSync(resolve(root, 'app.css'), 'utf8');
  const html = readFileSync(resolve(root, 'app.html'), 'utf8');

  /** `#111` and `#111111` are the same colour; compare them as such. */
  function expand(hex: string): string {
    const value = hex.trim().toLowerCase();
    return /^#[0-9a-f]{3}$/.test(value)
      ? `#${value[1]}${value[1]}${value[2]}${value[2]}${value[3]}${value[3]}`
      : value;
  }

  /** `--surface-base` from the dark (`:root`) and light theme blocks. */
  function surfaceBase(selector: string): string {
    const block = new RegExp(`${selector}\\s*\\{([\\s\\S]*?)\\}`, 'm').exec(css);
    expect(block, `no ${selector} block in app.css`).not.toBeNull();
    const declaration = /--surface-base:\s*([^;]+);/.exec(block![1]);
    expect(declaration, `no --surface-base in ${selector}`).not.toBeNull();
    return expand(declaration![1]);
  }

  it.each([
    ['dark', ':root', '#111111'],
    ['light', ":root\\[data-theme='light'\\]", '#ffffff'],
  ])('the %s chrome colour matches --surface-base', async (mode, selector, expected) => {
    expect(expand(expected)).toBe(surfaceBase(selector));

    addChromeMeta();
    const { applyTheme } = await load();
    applyTheme(mode as 'dark' | 'light');
    expect(expand(chromeColor()!)).toBe(expand(expected));
  });

  // The pre-paint script seeds the meta before the store loads; if it drifts,
  // the chrome flashes the wrong colour for one frame on every cold load.
  it.each([
    ['dark', '#111111'],
    ['light', '#ffffff'],
  ])('the app.html pre-paint script seeds the %s colour', (_mode, expected) => {
    expect(html).toContain(expected);
  });

  it('app.html carries a theme-color meta for the script to seed', () => {
    expect(html).toMatch(/<meta\s+name="theme-color"\s+content="[^"]+"\s*\/>/);
  });
});
