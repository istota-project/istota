/**
 * The bell and its count pill.
 *
 * The badge is the count of **open rows** — one number, no dot, no second
 * concept — because every open row means the same thing: something is waiting
 * on you, whether it is waiting to be done or waiting to be looked at.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, cleanup, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';

vi.mock('$lib/stores/notifications', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/stores/notifications')>();
  const { writable } = await import('svelte/store');
  return {
    ...actual,
    notificationCounts: writable({ open: 0, actionable: 0 }),
    notificationItems: writable([]),
    notificationTotalOpen: writable(0),
    notificationsLoading: writable(false),
    notificationsError: writable(''),
    refreshItems: vi.fn(),
    markPanelSeen: vi.fn(),
    runAction: vi.fn(),
    dismissNotification: vi.fn(),
  };
});

import { notificationCounts } from '$lib/stores/notifications';
import NotificationBell from './NotificationBell.svelte';
import CountPill from './CountPill.svelte';

type WritableCounts = { set(v: { open: number; actionable: number }): void };

function setCounts(open: number, actionable = open) {
  (notificationCounts as unknown as WritableCounts).set({ open, actionable });
}

afterEach(() => {
  cleanup();
  setCounts(0, 0);
});

describe('NotificationBell', () => {
  it('shows the open count', () => {
    setCounts(3);
    render(NotificationBell);
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('shows no badge at all when nothing is waiting', () => {
    setCounts(0);
    const { container } = render(NotificationBell);
    expect(container.querySelector('.count-pill')).toBeNull();
  });

  it('caps the rendered number at 99+', () => {
    setCounts(1240);
    render(NotificationBell);
    expect(screen.getByText('99+')).toBeInTheDocument();
  });

  it('carries the count in the accessible name', () => {
    // An icon plus a two-character glyph tells a screen reader nothing, so the
    // number goes in the name — and it is the *real* number, not the pill's
    // truncated form.
    setCounts(1240);
    render(NotificationBell);
    expect(screen.getByRole('button', { name: 'Notifications, 1240 waiting' })).toBeInTheDocument();
  });

  it('names itself plainly when there is nothing waiting', () => {
    setCounts(0);
    render(NotificationBell);
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeInTheDocument();
  });

  it('reads the count from the store rather than a prop', () => {
    setCounts(2);
    render(NotificationBell);
    expect(screen.getByText('2')).toBeInTheDocument();
    setCounts(5);
    // The bell is mounted in the root layout with no props; the poll is what
    // drives it, so a live store update has to reach the pill.
    expect(get(notificationCounts).open).toBe(5);
  });

  it('sits on the shared nav control class rather than restyling one', () => {
    setCounts(1);
    const { container } = render(NotificationBell);
    // Layout, hit area, reset and hover all come from `.nav-icon-btn` in
    // app-shell.css — a fourth control in that row must not fork them.
    expect(container.querySelector('button.nav-icon-btn')).toBeTruthy();
  });
});

describe('CountPill', () => {
  it('renders nothing at zero, so no call site needs its own {#if}', () => {
    const { container } = render(CountPill, { count: 0 });
    expect(container.querySelector('.count-pill')).toBeNull();
  });

  it('renders nothing for a negative count', () => {
    const { container } = render(CountPill, { count: -1 });
    expect(container.querySelector('.count-pill')).toBeNull();
  });

  it('renders the exact number up to 99', () => {
    render(CountPill, { count: 99 });
    expect(screen.getByText('99')).toBeInTheDocument();
  });

  it('caps at 99+ from 100', () => {
    render(CountPill, { count: 100 });
    expect(screen.getByText('99+')).toBeInTheDocument();
  });

  it('sets no aria-label, which a bare span cannot carry anyway', () => {
    // The implicit role of a <span> is `generic`, which does not support an
    // accessible name — AT drops the attribute. A prop named "screen-reader
    // name" that names nothing is worse than none, because it reads as covered.
    // The digits are text content, which a generic element does expose, and the
    // real untruncated count is on the bell button's own aria-label above.
    const { container } = render(CountPill, { count: 400 });
    const pill = container.querySelector('.count-pill');
    expect(pill).not.toBeNull();
    expect(pill!.hasAttribute('aria-label')).toBe(false);
    expect(pill!.textContent).toBe('99+');
  });

  it('still carries a native tooltip, which is valid on any element', () => {
    render(CountPill, { count: 4, title: '4 unread' });
    expect(screen.getByTitle('4 unread')).toBeInTheDocument();
  });

  it('sizes on --text-xs, the 0.7rem the chat chip hardcodes', () => {
    // Pinned against the source, like DraftCard's own max-width assertion:
    // jsdom does not apply a Svelte component's `<style>`, so a computed-style
    // check reads nothing whether the rule is there or not.
    //
    // The token matters. `.unread-chip` is 0.7rem and this is due to replace
    // it, so `--text-xs` makes that a true substitution with no visual change.
    // `--text-2xs` is 0.55rem and would shrink the chip by a fifth while a
    // "one fewer baselined violation" check still passed.
    const here = dirname(fileURLToPath(import.meta.url));
    const src = readFileSync(resolve(here, 'CountPill.svelte'), 'utf8');
    const style = src.slice(src.indexOf('<style'), src.lastIndexOf('</style>'));
    expect(style).toMatch(/font-size:\s*var\(--text-xs\)/);
    expect(style).not.toMatch(/--text-2xs/);
  });
});
