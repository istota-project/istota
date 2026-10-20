import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import Sidebar from './Sidebar.svelte';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const children = createRawSnippet(() => ({ render: () => '<p>rows</p>' }));

function atPhoneWidth(phone: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('max-width: 768px'),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

// Svelte sets `inert` as a property; a browser reflects it to the attribute and
// jsdom does not, so read either.
function isInert(el: HTMLElement) {
  return el.hasAttribute('inert') || el.inert === true;
}

function aside(open: boolean) {
  const { container } = render(Sidebar, { title: 'Rooms', open, onClose: () => {}, children });
  return container.querySelector('aside') as HTMLElement;
}

describe('Sidebar as a drawer', () => {
  // The closed drawer stays visible off-screen and is taken out of the tab
  // order and the accessibility tree by `inert`, so nothing but the transform
  // decides how the close looks (ISSUE-805).
  it('makes the closed drawer inert at phone width', () => {
    atPhoneWidth(true);
    expect(isInert(aside(false))).toBe(true);
  });

  it('leaves the open drawer reachable', () => {
    atPhoneWidth(true);
    expect(isInert(aside(true))).toBe(false);
  });

  it('never makes the desktop sidebar inert, open or not', () => {
    atPhoneWidth(false);
    expect(isInert(aside(false))).toBe(false);
  });

  it('renders where matchMedia is missing', () => {
    vi.stubGlobal('matchMedia', undefined);
    expect(isInert(aside(false))).toBe(false);
  });
});

describe('the drawer transition', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const source = readFileSync(resolve(here, 'Sidebar.svelte'), 'utf8');
  const drawerBlock = source.slice(source.indexOf('@media (max-width: 768px)'));
  const rule = (selector: string) =>
    drawerBlock.match(new RegExp(`\\n\\s*${selector.replace('.', '\\.')}\\s*\\{([^}]*)\\}`))?.[1] ??
    '';

  it('closes on the exit curve and duration', () => {
    expect(rule('.sidebar')).toMatch(
      /transition:\s*transform var\(--duration-exit\) var\(--ease-accelerate\)/,
    );
  });

  it('opens on the entrance curve, as before', () => {
    expect(rule('.sidebar.open')).toMatch(
      /transition:\s*transform var\(--duration-slow\) var\(--ease-decelerate\)/,
    );
  });

  it('draws no tap highlight on the full-screen backdrop', () => {
    expect(rule('.sidebar-backdrop')).toMatch(/-webkit-tap-highlight-color:\s*transparent/);
  });

  it('does not hide the drawer with visibility', () => {
    expect(rule('.sidebar')).not.toMatch(/visibility/);
    expect(rule('.sidebar.open')).not.toMatch(/visibility/);
  });
});
