/**
 * The notification inbox: the bell's count, the panel's rows, and the actions.
 *
 * Independent of `notices.ts`, which cannot back this. That store is transient
 * by design — `routes/+layout.svelte` calls `clearNotices()` on every
 * navigation, because a notice comments on the surface that raised it — and
 * nothing in it survives a reload. An inbox is the opposite: a set of items
 * that are still waiting on you whatever page you are on and however many times
 * you have reloaded.
 *
 * **The count is polled from the root layout, not from `AppShell`.** The bell
 * needs a number on every route and the shell is not on every route — the error
 * page renders none, and neither do the money loading branches. So the count
 * cannot ride on anything anchored to the shell.
 *
 * **The poll starts only once an authenticated user has resolved.** A
 * logged-out route polling an authenticated endpoint fails, backs off, and is
 * then indistinguishable from a real outage at the next login — so the layout
 * calls `startNotificationPoll()` when `getMe()` answers, and the loop stops
 * itself on the first `AuthError` rather than grinding on through an expired
 * session.
 */
import { writable, type Readable, type Writable } from 'svelte/store';
import {
  AuthError,
  getNotificationCounts,
  listNotifications,
  markNotificationsSeen,
  runNotificationAction,
  dismissNotification as apiDismissNotification,
  type NotificationAction,
  type NotificationCounts,
  type NotificationSeen,
  type ResolvedNotification,
} from '$lib/api';
import { notifyError } from './notices';

export type NotificationFilter = 'all' | 'action';

/** The ordinary cadence. Thirty seconds is the contract; the room stream's
 *  `notifications` frame is an optimisation on top of it and can be switched
 *  off entirely by `room_stream_room_check_seconds = 0`, so nothing may be
 *  tuned down on its account. */
export const POLL_INTERVAL_MS = 30_000;

/** Where the poll goes after a run of failures. A backed-off tab still recovers
 *  immediately on focus, which is the case that actually matters: the user has
 *  come back to look. */
export const BACKOFF_INTERVAL_MS = 5 * 60_000;

export const POLL_FAILURES_BEFORE_BACKOFF = 5;

/** How many rows the panel asks for. The liveness pass on the server covers the
 *  whole open set whatever this is, so the badge stays honest past the cut. */
export const PANEL_LIMIT = 50;

const counts = writable<NotificationCounts>({ open: 0, actionable: 0 });

/** Read-only outside this module: the count comes from the server, and a
 *  component setting it by hand would be writing a number nothing produced. */
export const notificationCounts: Readable<NotificationCounts> = {
  subscribe: counts.subscribe,
};

export const notificationItems: Writable<ResolvedNotification[]> = writable([]);

/** The honest open total behind the rendered page, for the panel's footer. */
export const notificationTotalOpen: Writable<number> = writable(0);

export const notificationsLoading: Writable<boolean> = writable(false);

/** A failed panel open, reported **in band** rather than as a `notify()`.
 *
 * The design language's rule is that a page which failed to load says so where
 * it can stay put and be re-read, and that reaching for a transient notice
 * there is a sign the surface is missing a banner. The panel now has one. */
export const notificationsError: Writable<string> = writable('');

let timer: ReturnType<typeof setTimeout> | null = null;
let polling = false;
let consecutiveFailures = 0;
let activeFilter: NotificationFilter = 'all';
let visibilityHandler: (() => void) | null = null;

function pollDelay(): number {
  return consecutiveFailures >= POLL_FAILURES_BEFORE_BACKOFF
    ? BACKOFF_INTERVAL_MS
    : POLL_INTERVAL_MS;
}

function arm() {
  if (!polling) return;
  if (timer !== null) clearTimeout(timer);
  timer = setTimeout(tick, pollDelay());
}

async function tick() {
  await refreshCounts();
  arm();
}

/** Fetch the badge. Failures are swallowed on purpose.
 *
 * A background poll is not something the user did, so it raises no notice —
 * the same reasoning the pending-confirmations poll has always used. What it
 * does instead is count: five in a row and the cadence drops to five minutes,
 * until a success or a focus event resets it.
 */
export async function refreshCounts(): Promise<void> {
  try {
    counts.set(await getNotificationCounts());
    consecutiveFailures = 0;
  } catch (e) {
    if (e instanceof AuthError) {
      // The session is gone. The layout redirects to login on its own; keeping
      // the loop running would spend the interval hammering a 401 and leave a
      // stale badge over a logged-out page.
      stopNotificationPoll();
      counts.set({ open: 0, actionable: 0 });
      return;
    }
    consecutiveFailures += 1;
  }
}

/** Fetch the panel's rows. Also corrects the badge, because it can.
 *
 * `total_open` is the post-sweep count of the whole open set, so writing it
 * onto the badge is what makes the number exact immediately after a panel open
 * rather than up to thirty seconds later. `actionable` is not in this payload —
 * the page is truncated and the filter may have cut it — so the count is
 * re-read rather than derived from the rows.
 */
export async function refreshItems(filter: NotificationFilter = activeFilter): Promise<void> {
  activeFilter = filter;
  notificationsLoading.set(true);
  try {
    const listing = await listNotifications(filter, PANEL_LIMIT);
    notificationItems.set(listing.notifications ?? []);
    notificationTotalOpen.set(listing.total_open ?? 0);
    counts.update((c) => ({ ...c, open: listing.total_open ?? 0 }));
    notificationsError.set('');
    consecutiveFailures = 0;
    void refreshCounts();
  } catch (e) {
    if (e instanceof AuthError) stopNotificationPoll();
    // Deliberately leaves whatever was on screen rather than blanking the list:
    // an empty panel reads as "nothing is waiting on you", which is the one
    // thing this feature must never say wrongly.
    notificationsError.set('Could not load your notifications.');
  } finally {
    notificationsLoading.set(false);
  }
}

function dropItem(id: number) {
  notificationItems.update((items) => items.filter((item) => item.id !== id));
  notificationTotalOpen.update((n) => Math.max(0, n - 1));
  counts.update((c) => ({ ...c, open: Math.max(0, c.open - 1) }));
}

/** Take an action a resolver offered on a row.
 *
 * The endpoint is the producer's own route — `/chat/tasks/12/confirm` — and is
 * checked against the path allowlist in `api.ts` before it is fetched. Removal
 * is pessimistic on success, like the confirmations banner: a row that vanished
 * and came back would read as the question having been asked twice.
 */
export async function runAction(id: number, action: NotificationAction): Promise<boolean> {
  if (action.method !== 'POST' || !action.endpoint) return false;
  try {
    await runNotificationAction(action.endpoint);
  } catch {
    notifyError('That action could not be completed. Try again.', {
      key: 'notifications:action',
    });
    // No optimistic removal on a failure: the item is still waiting, and this
    // panel is where the user can see that it is.
    void refreshItems();
    return false;
  }
  dropItem(id);
  void refreshItems();
  return true;
}

/** Clear a row by hand. "Not now", not "never again" — a later raise reopens it. */
export async function dismissNotification(id: number): Promise<boolean> {
  try {
    await apiDismissNotification(id);
  } catch {
    notifyError('Could not dismiss that notification.', { key: 'notifications:action' });
    return false;
  }
  dropItem(id);
  void refreshItems();
  return true;
}

/** Report what the panel rendered, as `(id, updated_at)` pairs.
 *
 * The version is not decoration. The server resolves a fire-and-forget row only
 * where the stored `updated_at` still matches the one sent, so an occurrence
 * raised between the fetch and this call is not closed by a user who never saw
 * it — and a retried POST arriving after the row reopened cannot close it
 * either.
 *
 * Silent on failure and it does **not** re-fetch the rows: an auto-resolving
 * item stays visible in the list the reader is looking at, and goes on the next
 * open. Closing a row out from under a mid-glance reader is worse than showing
 * it once more.
 */
export async function markPanelSeen(seen: NotificationSeen[]): Promise<void> {
  if (!seen.length) return;
  try {
    await markNotificationsSeen(seen);
  } catch {
    // Nothing the user did, and nothing they can fix. The next open re-sends.
    return;
  }
  void refreshCounts();
}

/** Begin the count poll. Idempotent; call it once the user has resolved. */
export function startNotificationPoll(): void {
  if (polling) return;
  polling = true;
  consecutiveFailures = 0;
  if (typeof document !== 'undefined') {
    // A background tab's timer is throttled and a suspended PWA's is stopped
    // outright, so returning to the app is both the moment a fresh count is
    // worth something and the moment a backed-off poll should be forgiven.
    visibilityHandler = () => {
      if (document.visibilityState !== 'visible') return;
      consecutiveFailures = 0;
      void refreshCounts();
      arm();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
  }
  void refreshCounts();
  arm();
}

export function stopNotificationPoll(): void {
  polling = false;
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
  if (visibilityHandler && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', visibilityHandler);
  }
  visibilityHandler = null;
}

/** Whether the loop is running. For the layout's teardown and for tests. */
export function isNotificationPollRunning(): boolean {
  return polling;
}

/** The filter the panel last asked for, so a post-action refresh keeps the tab. */
export function currentNotificationFilter(): NotificationFilter {
  return activeFilter;
}

/** Drop every trace of the signed-in user. Called on logout, and by tests. */
export function resetNotifications(): void {
  stopNotificationPoll();
  counts.set({ open: 0, actionable: 0 });
  notificationItems.set([]);
  notificationTotalOpen.set(0);
  notificationsLoading.set(false);
  notificationsError.set('');
  consecutiveFailures = 0;
  activeFilter = 'all';
}

/** The pairs to report for a rendered list. Exported so the panel and its test
 *  agree on the shape without either restating it. */
export function seenPairs(items: ResolvedNotification[]): NotificationSeen[] {
  return items.map((item) => ({ id: item.id, updated_at: item.updated_at }));
}

/** Tab labels come from the list response, never from the badge.
 *
 * `total_open` is the post-sweep total and the rows are what actually rendered,
 * so "Needs action (3)" can never sit above a visibly shorter list. The badge is
 * allowed to be briefly stale precisely because nothing is next to it to
 * contradict it; a label is.
 */
export function actionableCount(items: ResolvedNotification[]): number {
  return items.filter((item) => item.actionable).length;
}
