import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';
import AppShellHarness from './AppShell.notices.harness.svelte';
import { notify, clearNotices } from '$lib/stores/notices';

beforeEach(() => {
  window.matchMedia = ((query: string) => ({
    matches: query.includes('prefers-reduced-motion'),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
  clearNotices();
});

afterEach(() => {
  cleanup();
  clearNotices();
});

/**
 * The notice drawer is mounted by `AppShell` rather than by each page, which is
 * what makes `notify()` work from anywhere without a per-route render site.
 * These guard that arrangement: a page that stops getting a host would fail
 * silently, which is the exact failure class the notice layer exists to remove.
 */
describe('AppShell notice host', () => {
  it('carries a live region without any page opting in', () => {
    render(AppShellHarness);
    expect(screen.getByTestId('notice-region')).not.toBeNull();
  });

  it('renders a notice raised by code that never touched the shell', async () => {
    render(AppShellHarness);
    notify('Background sync finished', { severity: 'success' });
    expect(await screen.findByText('Background sync finished')).not.toBeNull();
  });

  // Anchored to the header, not the viewport: that is what keeps it clear of
  // the chat composer pinned to the bottom, and what makes the position the
  // same on every view.
  it('hangs the drawer inside the header band', async () => {
    const { container } = render(AppShellHarness);
    notify('Saved');
    await screen.findByText('Saved');
    const header = container.querySelector('.shell-header');
    expect(header?.contains(screen.getByTestId('notice-region'))).toBe(true);
  });
});

/**
 * The band overlays; it does not reflow.
 *
 * Asserted against the **source**, not a computed style: jsdom applies no
 * Svelte component `<style>` and performs no layout, so `getComputedStyle` and
 * `getBoundingClientRect` are both vacuous here — an assertion built on either
 * passes whether or not the rule exists. (The same reasoning `DraftCard`'s
 * max-width test records.)
 *
 * It is pinned because a caller now depends on it: chat states being offline
 * through a sticky notice precisely because the band costs the transcript no
 * height. A drawer that reflowed would silently reintroduce the problem that
 * change was made to fix, and the chat suite could not see it.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

describe('the notice band is out of flow', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const source = readFileSync(resolve(here, 'NoticeDrawer.svelte'), 'utf8');

  it('positions its region absolutely, so content beneath it does not move', () => {
    const rule = source.match(/\.notice-region\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(rule).toMatch(/position:\s*absolute/);
  });
});
