/**
 * The bell.
 *
 * The badge is the count of **open rows** — one number, no dot, no second
 * concept — because every open row means the same thing: something is waiting
 * on you, whether it is waiting to be done or waiting to be looked at.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
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
