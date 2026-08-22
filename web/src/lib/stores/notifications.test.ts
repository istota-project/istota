/**
 * The notification store: the count poll, its backoff, and the action verbs.
 *
 * The poll is the part with the most ways to be quietly wrong. It runs on every
 * route in the app, against an authenticated endpoint, from a tab that may be
 * backgrounded for a day — so the cases below are all failure shapes rather
 * than happy paths: starting before there is a session, failing silently,
 * failing five times, and coming back on focus.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({
  AuthError: class AuthError extends Error {},
  getNotificationCounts: vi.fn(),
  listNotifications: vi.fn(),
  markNotificationsSeen: vi.fn(),
  runNotificationAction: vi.fn(),
  dismissNotification: vi.fn(),
}));
vi.mock('$lib/api', () => api);

const notices = vi.hoisted(() => ({
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('./notices', () => notices);

type Store = typeof import('./notifications');

let store: Store;

function row(id: number, over: Record<string, unknown> = {}) {
  return {
    id,
    source: 'confirmation',
    severity: 'warning',
    actionable: true,
    title: `Question ${id}`,
    body: '',
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
    ],
    status_note: null,
    ...over,
  };
}

beforeEach(async () => {
  vi.resetModules();
  Object.values(api).forEach((v) => {
    if (typeof v === 'function' && 'mockReset' in v) (v as { mockReset(): void }).mockReset();
  });
  notices.notifyError.mockReset();
  api.getNotificationCounts.mockResolvedValue({ open: 0, actionable: 0 });
  api.listNotifications.mockResolvedValue({ notifications: [], total_open: 0 });
  api.markNotificationsSeen.mockResolvedValue({ status: 'ok' });
  api.runNotificationAction.mockResolvedValue({});
  api.dismissNotification.mockResolvedValue({ status: 'dismissed' });
  vi.useFakeTimers();
  store = await import('./notifications');
});

afterEach(() => {
  store.resetNotifications();
  vi.useRealTimers();
});

describe('the count poll', () => {
  it('does not run until it is started', async () => {
    // The layout starts it once `getMe()` has answered. Polling an
    // authenticated endpoint from a logged-out route fails, backs off, and is
    // then indistinguishable from a real outage at the next login.
    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS * 3);
    expect(api.getNotificationCounts).not.toHaveBeenCalled();
    expect(store.isNotificationPollRunning()).toBe(false);
  });

  it('fetches immediately on start and then on the interval', async () => {
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(3);
  });

  it('publishes the counts it fetched', async () => {
    api.getNotificationCounts.mockResolvedValue({ open: 4, actionable: 2 });
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.notificationCounts)).toEqual({ open: 4, actionable: 2 });
  });

  it('is idempotent, so a remount does not arm a second loop', async () => {
    store.startNotificationPoll();
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(2);
  });

  it('stops on request and arms nothing further', async () => {
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    store.stopNotificationPoll();
    api.getNotificationCounts.mockClear();
    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS * 5);
    expect(api.getNotificationCounts).not.toHaveBeenCalled();
  });

  it('stops itself and clears the badge when the session has gone', async () => {
    // The layout redirects to login on its own. Grinding on would spend the
    // interval hammering a 401 and leave a stale count over a logged-out page.
    api.getNotificationCounts.mockRejectedValue(new api.AuthError());
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.isNotificationPollRunning()).toBe(false);
    expect(get(store.notificationCounts)).toEqual({ open: 0, actionable: 0 });
  });

  it('swallows an ordinary poll failure', async () => {
    api.getNotificationCounts.mockRejectedValue(new Error('offline'));
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    // A background poll is not something the user did.
    expect(notices.notifyError).not.toHaveBeenCalled();
    expect(store.isNotificationPollRunning()).toBe(true);
  });
});

describe('backoff', () => {
  async function failNTimes(n: number) {
    api.getNotificationCounts.mockRejectedValue(new Error('offline'));
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    for (let i = 1; i < n; i += 1) {
      await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    }
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(n);
  }

  it('keeps the ordinary cadence below the threshold', async () => {
    await failNTimes(store.POLL_FAILURES_BEFORE_BACKOFF - 1);
    api.getNotificationCounts.mockClear();
    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(1);
  });

  it('drops to the long cadence after five consecutive failures', async () => {
    await failNTimes(store.POLL_FAILURES_BEFORE_BACKOFF);
    api.getNotificationCounts.mockClear();

    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    expect(api.getNotificationCounts).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(store.BACKOFF_INTERVAL_MS);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(1);
  });

  it('returns to the ordinary cadence after a success', async () => {
    await failNTimes(store.POLL_FAILURES_BEFORE_BACKOFF);
    api.getNotificationCounts.mockResolvedValue({ open: 1, actionable: 0 });
    await vi.advanceTimersByTimeAsync(store.BACKOFF_INTERVAL_MS);
    api.getNotificationCounts.mockClear();
    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(1);
  });
});

describe('visibility', () => {
  function becomeVisible(state: DocumentVisibilityState = 'visible') {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => state,
    });
    document.dispatchEvent(new Event('visibilitychange'));
  }

  it('refreshes immediately when the tab comes back', async () => {
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    api.getNotificationCounts.mockClear();
    becomeVisible('visible');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(1);
  });

  it('ignores the tab being hidden', async () => {
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    api.getNotificationCounts.mockClear();
    becomeVisible('hidden');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getNotificationCounts).not.toHaveBeenCalled();
  });

  it('forgives a backed-off poll, because the user has come back to look', async () => {
    api.getNotificationCounts.mockRejectedValue(new Error('offline'));
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    for (let i = 1; i < store.POLL_FAILURES_BEFORE_BACKOFF; i += 1) {
      await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    }
    api.getNotificationCounts.mockResolvedValue({ open: 2, actionable: 1 });

    becomeVisible('visible');
    await vi.advanceTimersByTimeAsync(0);
    api.getNotificationCounts.mockClear();

    // Back on the ordinary interval rather than still five minutes out.
    await vi.advanceTimersByTimeAsync(store.POLL_INTERVAL_MS);
    expect(api.getNotificationCounts).toHaveBeenCalledTimes(1);
  });

  it('stops listening once the poll is stopped', async () => {
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    store.stopNotificationPoll();
    api.getNotificationCounts.mockClear();
    becomeVisible('visible');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getNotificationCounts).not.toHaveBeenCalled();
  });
});

describe('refreshItems', () => {
  it('publishes the rows and the honest total', async () => {
    api.listNotifications.mockResolvedValue({
      notifications: [row(1), row(2)],
      total_open: 7,
    });
    await store.refreshItems('all');
    expect(get(store.notificationItems).map((i) => i.id)).toEqual([1, 2]);
    expect(get(store.notificationTotalOpen)).toBe(7);
  });

  it('corrects the badge from the post-sweep total', async () => {
    // The liveness pass runs on that request, so `total_open` is exact — which
    // is what makes the badge converge on a panel open rather than up to thirty
    // seconds later.
    api.getNotificationCounts.mockResolvedValue({ open: 60, actionable: 60 });
    store.startNotificationPoll();
    await vi.advanceTimersByTimeAsync(0);
    expect(get(store.notificationCounts).open).toBe(60);

    api.listNotifications.mockResolvedValue({ notifications: [row(1)], total_open: 1 });
    api.getNotificationCounts.mockResolvedValue({ open: 1, actionable: 1 });
    await store.refreshItems('all');
    expect(get(store.notificationCounts).open).toBe(1);
  });

  it('reports a failed load in band and keeps what was on screen', async () => {
    api.listNotifications.mockResolvedValue({ notifications: [row(1)], total_open: 1 });
    await store.refreshItems('all');

    api.listNotifications.mockRejectedValue(new Error('offline'));
    await store.refreshItems('all');

    // Blanking the list would say "nothing is waiting on you", which is the one
    // thing this feature must never say wrongly.
    expect(get(store.notificationItems)).toHaveLength(1);
    expect(get(store.notificationsError)).toBeTruthy();
    expect(notices.notifyError).not.toHaveBeenCalled();
  });

  it('clears a previous error on a successful load', async () => {
    api.listNotifications.mockRejectedValue(new Error('offline'));
    await store.refreshItems('all');
    api.listNotifications.mockResolvedValue({ notifications: [], total_open: 0 });
    await store.refreshItems('all');
    expect(get(store.notificationsError)).toBe('');
  });

  it('remembers the filter for a later refresh', async () => {
    await store.refreshItems('action');
    expect(store.currentNotificationFilter()).toBe('action');
    api.listNotifications.mockClear();
    await store.refreshItems();
    expect(api.listNotifications).toHaveBeenLastCalledWith('action', store.PANEL_LIMIT);
  });
});

describe('actions', () => {
  it('posts the exact path the view named', async () => {
    const item = row(9);
    await store.runAction(item.id, item.actions[0] as never);
    expect(api.runNotificationAction).toHaveBeenCalledWith('/chat/tasks/9/confirm');
  });

  it('drops the row and refreshes on success', async () => {
    api.listNotifications.mockResolvedValue({ notifications: [row(1)], total_open: 1 });
    await store.refreshItems('all');
    const item = get(store.notificationItems)[0];

    api.listNotifications.mockResolvedValue({ notifications: [], total_open: 0 });
    await store.runAction(item.id, item.actions[0]);
    // Pessimistic: a row that vanished and came back would read as the question
    // having been asked twice.
    expect(get(store.notificationItems)).toEqual([]);
    expect(api.listNotifications).toHaveBeenCalledTimes(2);
  });

  it('keeps the row and raises a notice when the action fails', async () => {
    api.listNotifications.mockResolvedValue({ notifications: [row(1)], total_open: 1 });
    await store.refreshItems('all');
    const item = get(store.notificationItems)[0];

    api.runNotificationAction.mockRejectedValue(new Error('boom'));
    const ok = await store.runAction(item.id, item.actions[0]);
    expect(ok).toBe(false);
    expect(notices.notifyError).toHaveBeenCalledWith(expect.any(String), {
      key: 'notifications:action',
    });
    expect(get(store.notificationItems)).toHaveLength(1);
  });

  it('refuses a LINK action rather than POSTing its href', async () => {
    const ok = await store.runAction(1, {
      id: 'open',
      label: 'Open',
      kind: 'default',
      method: 'LINK',
      endpoint: null,
      href: '/health',
    });
    expect(ok).toBe(false);
    expect(api.runNotificationAction).not.toHaveBeenCalled();
  });

  it('dismisses a row', async () => {
    api.listNotifications.mockResolvedValue({ notifications: [row(1)], total_open: 1 });
    await store.refreshItems('all');
    api.listNotifications.mockResolvedValue({ notifications: [], total_open: 0 });
    expect(await store.dismissNotification(1)).toBe(true);
    expect(api.dismissNotification).toHaveBeenCalledWith(1);
    expect(get(store.notificationItems)).toEqual([]);
  });
});

describe('markPanelSeen', () => {
  it('sends (id, updated_at) pairs for exactly the rendered rows', async () => {
    const items = [row(1), row(2)];
    await store.markPanelSeen(store.seenPairs(items as never));
    expect(api.markNotificationsSeen).toHaveBeenCalledWith([
      { id: 1, updated_at: '2026-08-01T00:00:00.000Z' },
      { id: 2, updated_at: '2026-08-02T00:00:00.000Z' },
    ]);
  });

  it('sends nothing for an empty panel', async () => {
    await store.markPanelSeen([]);
    expect(api.markNotificationsSeen).not.toHaveBeenCalled();
  });

  it('does not re-fetch the rows', async () => {
    // A fire-and-forget row this call resolves stays visible in the list the
    // reader is looking at. Closing one out from under a mid-glance reader is
    // worse than showing it once more.
    api.listNotifications.mockResolvedValue({ notifications: [row(1)], total_open: 1 });
    await store.refreshItems('all');
    api.listNotifications.mockClear();
    await store.markPanelSeen(store.seenPairs(get(store.notificationItems)));
    expect(api.listNotifications).not.toHaveBeenCalled();
  });

  it('is silent when the report fails', async () => {
    api.markNotificationsSeen.mockRejectedValue(new Error('offline'));
    await store.markPanelSeen([{ id: 1, updated_at: 'x' }]);
    expect(notices.notifyError).not.toHaveBeenCalled();
  });
});

describe('derived tab counts', () => {
  it('counts only the actionable rows that rendered', () => {
    const items = [row(1), row(2, { actionable: false }), row(3)];
    expect(store.actionableCount(items as never)).toBe(2);
  });
});
