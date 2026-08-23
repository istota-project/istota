/**
 * The notification panel.
 *
 * Rendered through `NotificationBell`, which is its only caller and supplies the
 * trigger snippet bits-ui needs a real element for — a raw snippet cannot spread
 * the trigger props onto one with working handlers, and testing the panel with a
 * fake trigger would be testing a shape nothing mounts.
 *
 * bits-ui opens its floating content on pointerdown, which jsdom only partly
 * implements; the keyboard path is equivalent and fully supported here, which is
 * the same route `KebabMenu.svelte.test.ts` takes.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { ResolvedNotification } from '$lib/api';

vi.mock('$lib/stores/notifications', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/stores/notifications')>();
  const { writable } = await import('svelte/store');
  return {
    ...actual,
    notificationCounts: writable({ open: 0, actionable: 0 }),
    notificationItems: writable([] as ResolvedNotification[]),
    notificationTotalOpen: writable(0),
    notificationsLoading: writable(false),
    notificationsError: writable(''),
    refreshItems: vi.fn(),
    markPanelSeen: vi.fn(),
    runAction: vi.fn(),
    dismissNotification: vi.fn(),
  };
});

import {
  dismissNotification,
  markPanelSeen,
  notificationCounts,
  notificationItems,
  notificationTotalOpen,
  notificationsError,
  notificationsLoading,
  refreshItems,
  runAction,
} from '$lib/stores/notifications';
import NotificationBell from './NotificationBell.svelte';

type AnyWritable = { set(v: unknown): void };
const set = (store: unknown, value: unknown) => (store as AnyWritable).set(value);

function row(id: number, over: Partial<ResolvedNotification> = {}): ResolvedNotification {
  return {
    id,
    source: 'confirmation',
    severity: 'warning',
    actionable: true,
    title: `Question ${id}`,
    body: 'the whole question',
    link: null,
    occurrences: 1,
    created_at: '2026-08-01T00:00:00.000Z',
    updated_at: `2026-08-0${id}T00:00:00.000Z`,
    seen_at: null,
    object_type: 'task',
    object_id: String(id),
    actions: [
      {
        id: 'confirm',
        label: 'Confirm',
        kind: 'primary',
        method: 'POST',
        endpoint: `/chat/tasks/${id}/confirm`,
        href: null,
      },
      {
        id: 'discard',
        label: 'Discard',
        kind: 'danger',
        method: 'POST',
        endpoint: `/chat/tasks/${id}/cancel`,
        href: null,
      },
    ],
    status_note: null,
    ...over,
  };
}

async function openPanel() {
  render(NotificationBell);
  const trigger = screen.getByRole('button', { name: /Notifications/ });
  await fireEvent.keyDown(trigger, { key: 'Enter' });
  return trigger;
}

beforeEach(() => {
  vi.mocked(refreshItems).mockReset();
  // The real one returns the rows it published, or null when the load failed or
  // was superseded — the panel marks seen from that value rather than from the
  // store, so the double must honour the same contract or every seen assertion
  // below passes vacuously.
  vi.mocked(refreshItems).mockImplementation(async () => get(notificationItems));
  vi.mocked(markPanelSeen).mockReset();
  vi.mocked(runAction).mockReset();
  vi.mocked(dismissNotification).mockReset();
  set(notificationItems, []);
  set(notificationTotalOpen, 0);
  set(notificationCounts, { open: 0, actionable: 0 });
  set(notificationsError, '');
  set(notificationsLoading, false);
});

afterEach(cleanup);

describe('opening', () => {
  it('loads the rows, on the All filter', async () => {
    // "All" is the landing state and that is load-bearing rather than a
    // default: a fire-and-forget row has no object whose change would close it,
    // so being rendered is what closes it. Landing on "Needs action" would make
    // that class never render, never resolve, and climb forever.
    await openPanel();
    expect(refreshItems).toHaveBeenCalledWith('all');
  });

  it('reports what it rendered as (id, updated_at) pairs', async () => {
    set(notificationItems, [row(1), row(2)]);
    await openPanel();
    expect(markPanelSeen).toHaveBeenCalledWith([
      { id: 1, updated_at: '2026-08-01T00:00:00.000Z' },
      { id: 2, updated_at: '2026-08-02T00:00:00.000Z' },
    ]);
  });

  it('reports nothing when the load failed', async () => {
    // `refreshItems` leaves the previous rows on screen on failure, so reading
    // the store here would stamp `seen_at` on a list the user is not being
    // shown, behind an error banner.
    set(notificationItems, [row(1)]);
    vi.mocked(refreshItems).mockResolvedValue(null);
    await openPanel();
    expect(markPanelSeen).not.toHaveBeenCalled();
  });

  it('reports only the rows it actually rendered', async () => {
    set(notificationItems, [row(3)]);
    await openPanel();
    expect(markPanelSeen).toHaveBeenCalledWith([{ id: 3, updated_at: '2026-08-03T00:00:00.000Z' }]);
  });
});

describe('the list', () => {
  it('renders a row per notification', async () => {
    set(notificationItems, [row(1), row(2)]);
    await openPanel();
    expect(await screen.findByText('Question 1')).toBeInTheDocument();
    expect(screen.getByText('Question 2')).toBeInTheDocument();
  });

  it('says nothing is waiting when empty', async () => {
    await openPanel();
    expect(await screen.findByText('Nothing waiting on you.')).toBeInTheDocument();
  });

  it('marks a repeated row with its occurrence count', async () => {
    set(notificationItems, [row(1, { occurrences: 4 })]);
    await openPanel();
    expect(await screen.findByText('×4')).toBeInTheDocument();
  });

  it('shows no occurrence marker for a row that fired once', async () => {
    set(notificationItems, [row(1)]);
    await openPanel();
    await screen.findByText('Question 1');
    expect(screen.queryByText('×1')).toBeNull();
  });

  it('reports a failed load in band rather than as an empty panel', async () => {
    set(notificationsError, 'Could not load your notifications.');
    await openPanel();
    expect(await screen.findByText('Could not load your notifications.')).toBeInTheDocument();
  });

  it('states the total when the page is shorter than the open set', async () => {
    set(notificationItems, [row(1)]);
    set(notificationTotalOpen, 60);
    await openPanel();
    expect(await screen.findByText(/Showing 1 of 60 open/)).toBeInTheDocument();
  });

  it('states it on the filtered tab too, which is the one that hides rows', async () => {
    // Gated on `all`, the one tab that can show a short list without saying why
    // was the filtered one.
    set(notificationItems, [row(1)]);
    set(notificationTotalOpen, 60);
    await openPanel();
    await fireEvent.click(await screen.findByText(/Needs action/));
    expect(await screen.findByText(/Showing 1 of 60 open/)).toBeInTheDocument();
  });

  it('says nothing about a total the page already shows in full', async () => {
    set(notificationItems, [row(1)]);
    set(notificationTotalOpen, 1);
    await openPanel();
    await screen.findByText('Question 1');
    expect(screen.queryByText(/Showing/)).toBeNull();
  });
});

describe('the filter tabs', () => {
  it('labels each tab from the list response, not from the badge', async () => {
    // `total_open` is the post-sweep total and the rows are what rendered, so a
    // label reading "Needs action (3)" cannot sit above a shorter list. The
    // badge is allowed to be briefly stale; a label sitting on top of the list
    // it describes is not.
    set(notificationCounts, { open: 99, actionable: 99 });
    set(notificationItems, [row(1), row(2, { actionable: false })]);
    set(notificationTotalOpen, 2);
    await openPanel();
    expect(await screen.findByText('All (2)')).toBeInTheDocument();
    expect(screen.getByText('Needs action (1)')).toBeInTheDocument();
  });

  it('re-fetches on the selected filter', async () => {
    set(notificationItems, [row(1)]);
    await openPanel();
    await fireEvent.click(await screen.findByText(/Needs action/));
    expect(refreshItems).toHaveBeenLastCalledWith('action');
  });

  it('lands back on All on the next open, not on the filter last picked', async () => {
    // The component stays mounted for the life of the tab, so a filter that
    // persisted would make "Needs action" sticky — and the whole reason for the
    // All default is that *opening the bell* renders both classes. Sticky, a
    // fire-and-forget row would never render for that user, never be seen,
    // never resolve, and climb until the 14-day sweep caught it.
    const trigger = await openPanel();
    await fireEvent.click(await screen.findByText(/Needs action/));
    expect(refreshItems).toHaveBeenLastCalledWith('action');

    await fireEvent.keyDown(trigger, { key: 'Escape' });
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(refreshItems).toHaveBeenLastCalledWith('all');
  });

  it('says something different when the action tab is empty', async () => {
    set(notificationItems, [row(1)]);
    await openPanel();
    await fireEvent.click(await screen.findByText(/Needs action/));
    set(notificationItems, []);
    expect(await screen.findByText('Nothing needs your attention.')).toBeInTheDocument();
  });
});

describe('actions', () => {
  it('issues the exact path the view named', async () => {
    // The endpoint is the producer's own route, chosen by the resolver. There
    // is no dispatcher in front of it, so what the client posts has to be what
    // the payload said.
    set(notificationItems, [row(7)]);
    await openPanel();
    await fireEvent.click(await screen.findByText('Confirm'));
    expect(runAction).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ id: 'confirm', endpoint: '/chat/tasks/7/confirm' }),
    );
  });

  it('shows at most two actions inline', async () => {
    set(notificationItems, [
      row(1, {
        actions: [
          ...row(1).actions,
          {
            id: 'third',
            label: 'Third',
            kind: 'default',
            method: 'POST',
            endpoint: '/chat/tasks/1/third',
            href: null,
          },
        ],
      }),
    ]);
    await openPanel();
    await screen.findByText('Confirm');
    // Past two the row is a form rather than a line in a list; the rest live in
    // the detail modal, which renders every action there is.
    expect(screen.queryByText('Third')).toBeNull();
  });

  it('does not let an unusable action consume an inline slot', async () => {
    // Sliced before filtering, a rejected LINK would eat one of the two slots
    // and render nothing — one button where two were intended, with nothing
    // saying an action had been dropped.
    set(notificationItems, [
      row(1, {
        actions: [
          {
            id: 'bad',
            label: 'Bad link',
            kind: 'default',
            method: 'LINK',
            endpoint: null,
            href: 'https://evil.example',
          },
          ...row(1).actions,
        ],
      }),
    ]);
    await openPanel();
    expect(await screen.findByText('Confirm')).toBeInTheDocument();
    expect(screen.getByText('Discard')).toBeInTheDocument();
    expect(screen.queryByText('Bad link')).toBeNull();
  });

  it('opens the detail modal from the row', async () => {
    set(notificationItems, [row(1)]);
    await openPanel();
    await fireEvent.click(await screen.findByLabelText('Open notification: Question 1'));
    expect(await screen.findByText('the whole question')).toBeInTheDocument();
  });

  it('closes the panel behind the modal', async () => {
    // A list and the detail of one of its rows are the same thing twice, and on
    // a phone it spans nearly the whole viewport -- with `--z-popover` (100)
    // deliberately above `--z-modal` (50) so a Select inside a dialog clears the
    // dialog, the modal rendered *under* the panel there. Closing the panel
    // removes the overlap rather than re-ordering the z-scale for everyone.
    set(notificationItems, [row(1)]);
    await openPanel();
    expect(screen.getByText('Notifications')).toBeInTheDocument();
    await fireEvent.click(await screen.findByLabelText('Open notification: Question 1'));
    // The modal is up...
    expect(await screen.findByText('the whole question')).toBeInTheDocument();
    // ...and the panel behind it is gone. `queryByText` because absence is the
    // assertion; `getByText` would throw before it could report.
    expect(screen.queryByText('Notifications')).not.toBeInTheDocument();
  });

  it('dismisses from the detail modal', async () => {
    // No actions: Dismiss is offered only where the row has nothing else that
    // would clear it, so a row carrying Confirm/Discard has no Dismiss to click.
    set(notificationItems, [row(1, { actions: [], status_note: 'Nothing to do.' })]);
    await openPanel();
    await fireEvent.click(await screen.findByLabelText('Open notification: Question 1'));
    await fireEvent.click(await screen.findByText('Dismiss'));
    expect(dismissNotification).toHaveBeenCalledWith(1);
  });
});

describe('the phone sheet', () => {
  // These two assertions read the component source, which is not how anything
  // else here is tested, and the reason is a limit of the runner rather than a
  // preference: jsdom drops `@media` blocks from the cascade entirely -- a rule
  // behind a query that *matches* is still not applied -- and floating-ui does
  // no layout in it either. There is no arrangement of this suite in which the
  // panel has a measurable position.
  //
  // What is left to guard is the coupling, and the coupling is the whole trick:
  // the collision padding bounds the shift to `[gutter, 100vw - gutter]` and the
  // phone width is that entire span, so the clamp has exactly one solution and
  // the sheet centres itself wherever the bell happens to sit. Drop either half
  // and it goes back to resting against one edge with the gutter stranded on the
  // other -- which is what it did before, and what nothing in a jsdom suite can
  // see. Verified by hand in the browser at 318 / 388 / 498 / 598 / 765 px and
  // at both text-scale settings: 12px each side at every one.
  // `process.cwd()`-relative, matching `lib/styles/cascade.ts` -- vitest runs
  // from `web/`, and `import.meta.url` is not a file URL under vite.
  const source = readFileSync(
    join(process.cwd(), 'src/lib/components/ui/NotificationPanel.svelte'),
    'utf-8',
  );

  it('bounds the shift with the same constant the phone width is derived from', () => {
    expect(source).toMatch(/const VIEWPORT_GUTTER = \d+;/);
    expect(source).toMatch(/collisionPadding=\{VIEWPORT_GUTTER\}/);
  });

  it('takes the phone width from floating-ui rather than restating it', () => {
    // `--bits-popover-content-available-width` *is* viewport-minus-collision-
    // padding, measured by the middleware that does the clamping. A hand-written
    // `calc(100vw - 24px)` would be a second spelling of the bound that can
    // disagree with it -- over a scrollbar, which `100vw` counts and floating-ui
    // does not, and over anyone who edits one number and not the other.
    //
    // Scoped to the phone block rather than to the file: the base rule's
    // `max-width: calc(100vw - var(--space-4))` is a legitimate outer guard and
    // ends in the same six characters, so a file-wide negative match flags it.
    const phoneRule = source.slice(source.indexOf('@media (max-width: 768px)'));
    expect(phoneRule).toMatch(/[\s;{]width:\s*var\(--bits-popover-content-available-width/);
    expect(phoneRule).not.toMatch(/[\s;{]width:\s*calc\(/);
  });
});
