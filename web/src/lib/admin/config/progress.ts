/**
 * The timings a submitted config change is followed with, in one place so the
 * component test can step through them.
 */

/** Status poll interval while the host has the request. */
export const POLL_MS = 2_000;

/** A request still `pending` after this long has not been picked up: the
 *  Health pane's threshold and wording for an unanswered doctor run. */
export const NOT_PICKED_UP_MS = 120_000;

/** How long to wait for the web process to come back after an applied
 *  change before saying it has not, and naming the host recovery. */
export const RESTART_DEADLINE_MS = 180_000;

/** The wait before retry `n` (1-based) after a failed request: the restart
 *  takes the web process down, so failures are expected for a while. */
export function backoffMs(n: number): number {
  return Math.min(1_000 * 2 ** Math.max(0, n - 1), 15_000);
}

/** Whether the process that answered started after the change finished,
 *  which is what says the restart happened. */
export function restartedAfter(startedAt: string | null, finishedAt: string | null): boolean {
  if (!startedAt || !finishedAt) return false;
  const started = Date.parse(startedAt);
  const finished = Date.parse(finishedAt);
  return Number.isFinite(started) && Number.isFinite(finished) && started > finished;
}
