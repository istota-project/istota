import { derived, get, writable, type Readable } from 'svelte/store';

/**
 * The app's transient feedback channel — one `notify()` call from anywhere,
 * rendered once by the notice drawer that `AppShell` hangs under the section
 * header.
 *
 * What belongs here is **out-of-band** feedback: something happened that has no
 * natural home on the page. A background sync failed, a share link was copied,
 * an optimistic update was rolled back. What does *not* belong here is in-band
 * state tied to a specific object on screen — a failed message send belongs on
 * its own bubble, a form's validation error belongs under its field, and a
 * page that failed to load belongs in that page's banner where it can stay put
 * and be re-read. Routing those through a notice would double-report the
 * failure and then take the report away after four seconds.
 *
 * Where a surface has no banner to put such a failure in, a notice beats
 * silence — but the banner is the better fix, and reaching for one here is a
 * note that the surface is missing it.
 *
 * Timers live here rather than in the drawer, so the queue behaves identically
 * whether or not anything is currently rendering it, and so tests can drive it
 * with fake timers without mounting a component.
 */

export type NoticeSeverity = 'info' | 'success' | 'warning' | 'error';

export interface NoticeAction {
  label: string;
  run: () => void;
}

export interface NoticeOptions {
  severity?: NoticeSeverity;
  /**
   * Milliseconds on screen. `0` means it stays until dismissed. Omit to take
   * the severity default.
   */
  duration?: number;
  /** A single affordance — "Retry", "Undo", "Reload". */
  action?: NoticeAction;
  /**
   * Coalescing identity. Defaults to severity + message, which is what makes a
   * burst of identical network errors one counted notice instead of N copies.
   * Pass an explicit key to coalesce notices whose text differs but which are
   * the same event ("retrying in 3s" → "retrying in 2s").
   */
  key?: string;
  /**
   * This notice states a **condition**, not an event: it is true for as long as
   * it is true, and the thing that raised it is what takes it down.
   *
   * Everything else in this file assumes an event — something that happened,
   * which the user reads once. Three of those assumptions are wrong for a
   * condition and each would take it off screen while it still held: the
   * navigation clear, the 30s handover, and the queue trim. A sticky notice is
   * exempt from all three.
   *
   * It is not exempt from *yielding*. Holding the one slot for the life of the
   * condition is the hazard `PINNED_HANDOVER_MS` exists to prevent, so a sticky
   * head steps aside for any event waiting behind it and takes the slot back
   * when they are done — see `yieldStickyHead`.
   *
   * Reach for it only where the raiser can prove the condition ended. Today
   * that is chat's offline notice, driven off the connectivity store.
   */
  sticky?: boolean;
}

export interface Notice {
  id: number;
  message: string;
  severity: NoticeSeverity;
  duration: number;
  action?: NoticeAction;
  key: string;
  /** How many times this notice has been raised, counting the first. */
  count: number;
  /** A condition rather than an event. See `NoticeOptions.sticky`. */
  sticky: boolean;
}

/**
 * How long each severity stays up. An error is `0` — dismiss-only — because it
 * names something that did not happen, and a four-second window is easy to
 * miss; the other three are announcements the user can safely not read.
 */
export const DURATIONS: Record<NoticeSeverity, number> = {
  info: 4000,
  success: 4000,
  warning: 6000,
  error: 0,
};

/**
 * How long a pinned notice may hold the slot once something is waiting behind
 * it. "Dismiss-only" is about giving the user time to acknowledge, not about
 * owning the channel: without this, one unanswered error makes every later
 * notice invisible for the life of the tab.
 */
export const PINNED_HANDOVER_MS = 30000;

/**
 * Ceiling on the backlog. A pinned head plus a flapping poller would otherwise
 * grow the queue without limit and then replay it one dismissal at a time.
 */
export const MAX_QUEUE = 5;

const queue = writable<Notice[]>([]);

/** The whole queue, oldest first. Chiefly for tests and diagnostics. */
export const notices: Readable<Notice[]> = { subscribe: queue.subscribe };

/**
 * The notice on screen. The drawer is one slot under the header, so concurrent
 * notices queue rather than stack — a column of them would cover the content
 * they are commenting on.
 */
export const currentNotice: Readable<Notice | null> = derived(queue, (list) => list[0] ?? null);

let nextId = 1;
let timer: ReturnType<typeof setTimeout> | undefined;
/** The notice the live timer belongs to, so a re-arm can tell "same" from "new". */
let timedId: number | undefined;

function clearTimer(): void {
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
  timedId = undefined;
}

/**
 * A sticky head steps aside for events waiting behind it.
 *
 * The alternative for a condition that outlives every event around it is one of
 * two bad ones: hold the slot and silence the channel for as long as the
 * condition lasts, or take the handover and be dismissed while still true. So a
 * sticky notice yields instead — it goes to the back, the events ahead of it
 * run their course, and it returns to the head when the queue drains to it.
 *
 * Rotating immediately rather than after `PINNED_HANDOVER_MS` is the point: an
 * event queued behind a condition would otherwise wait 30s to be seen, which is
 * the same silencing by a slower route.
 *
 * Only ever rotates past a non-sticky entry, so an all-sticky queue terminates.
 */
function yieldStickyHead(list: Notice[]): Notice[] {
  if (list.length < 2) return list;
  if (!list[0].sticky) return list;
  if (!list.some((n) => !n.sticky)) return list;
  return [...list.slice(1), list[0]];
}

/** How long the head may hold the slot; 0 means until it is dismissed. */
function headLifetime(list: Notice[]): number {
  const current = list[0];
  if (current.duration > 0) return current.duration;
  // A condition is taken down by whatever raised it, never by a clock.
  if (current.sticky) return 0;
  // A pinned notice keeps the slot while it is the only thing waiting. The
  // moment something queues behind it, holding on indefinitely would silence
  // that one and every one after it, so it hands over after a long grace.
  if (list.length > 1) return PINNED_HANDOVER_MS;
  return 0;
}

/**
 * Keep the timer pointed at whatever is currently on screen. Called after every
 * queue change: a queued notice must not burn its lifetime unseen, so its clock
 * only starts when it reaches the head.
 */
function armTimer(restart = false): void {
  const list = get(queue);
  const current = list[0] ?? null;
  if (!current) {
    clearTimer();
    return;
  }

  const lifetime = headLifetime(list);

  if (lifetime <= 0) {
    clearTimer();
    return;
  }
  // Already counting down for this notice, and nothing asked for a reset.
  if (timedId === current.id && !restart) return;

  clearTimer();
  timedId = current.id;
  timer = setTimeout(() => dismissNotice(current.id), lifetime);
}

function resolveDuration(
  options: NoticeOptions,
  severity: NoticeSeverity,
  action: NoticeAction | undefined,
): number {
  if (options.duration !== undefined) return Math.max(0, options.duration);
  // A notice carrying an action is a decision to make, not an announcement.
  // Expiring it out from under a reaching finger removes the only way to take
  // it, so an action pins the notice until it is answered or dismissed.
  if (action) return 0;
  return DURATIONS[severity];
}

/**
 * Raise a notice. Returns its id, which `dismissNotice` takes — a caller that
 * pinned a notice with `duration: 0` uses it to take the notice down once the
 * thing it was about resolves.
 */
export function notify(message: string, options: NoticeOptions = {}): number {
  const severity = options.severity ?? 'info';
  const key = options.key ?? `${severity}:${message}`;

  let id = 0;
  let coalescedIntoHead = false;

  queue.update((list) => {
    const index = list.findIndex((n) => n.key === key);
    if (index >= 0) {
      const existing = list[index];
      id = existing.id;
      coalescedIntoHead = index === 0;

      // Anything the repeat states wins; anything it omits is left alone. The
      // distinction matters on an explicit `key`, which is the whole point of
      // that option: a progress repeat that says nothing about severity or
      // action must not quietly downgrade an error to info, nor delete the
      // Retry button the first call offered. (On the default key both are
      // moot — the key embeds the severity, so a match implies the same one.)
      const nextSeverity = options.severity ?? existing.severity;
      const nextAction = 'action' in options ? options.action : existing.action;

      const next = [...list];
      next[index] = {
        ...existing,
        message,
        severity: nextSeverity,
        action: nextAction,
        // Resolved against what the notice actually ends up carrying, so
        // keeping an action keeps the pin that action implies.
        duration: resolveDuration(options, nextSeverity, nextAction),
        count: existing.count + 1,
      };
      return next;
    }

    id = nextId++;
    const next = [
      ...list,
      {
        id,
        message,
        severity,
        duration: resolveDuration(options, severity, options.action),
        action: options.action,
        key,
        count: 1,
        sticky: options.sticky ?? false,
      },
    ];
    // Drop the oldest *queued* notice rather than the visible one or the
    // newest: the user is reading the head, and the newest is the most
    // relevant thing that just happened. A sticky one is never the victim —
    // it is a condition that is still true, and nothing would raise it again.
    while (next.length > MAX_QUEUE) {
      const victim = next.findIndex((n, i) => i > 0 && !n.sticky);
      if (victim < 0) break;
      next.splice(victim, 1);
    }
    return yieldStickyHead(next);
  });

  // A repeat of the visible notice restarts its clock — the event is still
  // happening, so the user gets the full window from the latest occurrence.
  armTimer(coalescedIntoHead);
  return id;
}

/** Take down a specific notice. A stale id is a no-op. */
export function dismissNotice(id: number): void {
  // Re-checked after the removal: dismissing the event a sticky notice stepped
  // aside for is exactly when the condition should take the slot back.
  queue.update((list) => yieldStickyHead(list.filter((n) => n.id !== id)));
  armTimer();
}

/**
 * Drop every notice, live timer included — except a sticky one, which states a
 * condition rather than commenting on the surface being navigated away from.
 * The raiser is what takes those down.
 */
export function clearNotices(): void {
  clearTimer();
  queue.update((list) => list.filter((n) => n.sticky));
  armTimer();
}

export const notifyInfo = (message: string, options: NoticeOptions = {}) =>
  notify(message, { ...options, severity: 'info' });

export const notifySuccess = (message: string, options: NoticeOptions = {}) =>
  notify(message, { ...options, severity: 'success' });

export const notifyWarning = (message: string, options: NoticeOptions = {}) =>
  notify(message, { ...options, severity: 'warning' });

export const notifyError = (message: string, options: NoticeOptions = {}) =>
  notify(message, { ...options, severity: 'error' });
