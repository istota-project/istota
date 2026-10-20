import { getAdminDoctor, requestDoctorRun, type DoctorReport } from '$lib/api';

// The daemon answers a request on its next tick (a second or so) and a deep
// run spawns a sandbox namespace, so this is generous; past it the daemon is
// not picking requests up, which is worth saying rather than spinning on.
export const POLL_MS = 1500;
export const WAIT_MS = 120_000;

export interface DoctorRunResult {
  /** Whether the daemon finished the request within `WAIT_MS`. */
  answered: boolean;
  /** Whether the wait ended because its signal aborted (the caller went away). */
  cancelled: boolean;
  /** Whether the request that was joined or made is a deep one. */
  deep: boolean;
  /** The last report read; null only if the wait ended before the first poll. */
  report: DoctorReport | null;
}

export interface DoctorRunOptions {
  /** Sees every report read on the way, so a page can show the latest one. */
  onReport?: (report: DoctorReport) => void;
  /** Aborted on unmount: the wait stops, sends nothing more and reports nothing. */
  signal?: AbortSignal;
}

function pause(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal?.addEventListener('abort', done, { once: true });
  });
}

/**
 * Ask the daemon for a doctor run and poll `/admin/doctor` until it answers
 * that request or `WAIT_MS` passes. Errors from either call propagate, except
 * once `signal` has aborted: then the result is `cancelled` and nothing is
 * thrown, so a caller that has gone raises no notice on another page.
 */
export async function waitForDoctorRun(
  deep: boolean,
  { onReport, signal }: DoctorRunOptions = {},
): Promise<DoctorRunResult> {
  let report: DoctorReport | null = null;
  let askedDeep = deep;
  const cancelled = (): DoctorRunResult => ({
    answered: false,
    cancelled: true,
    deep: askedDeep,
    report,
  });
  try {
    const asked = await requestDoctorRun(deep);
    askedDeep = asked.deep;
    if (signal?.aborted) return cancelled();
    const until = Date.now() + WAIT_MS;
    while (Date.now() < until) {
      await pause(POLL_MS, signal);
      if (signal?.aborted) return cancelled();
      const next = await getAdminDoctor();
      if (signal?.aborted) return cancelled();
      report = next;
      onReport?.(report);
      // The daemon's own marker, not a report's request_id: the next sweep
      // overwrites that, and a failed run still answers.
      if (report.answered?.id === asked.id) {
        return { answered: true, cancelled: false, deep: asked.deep, report };
      }
    }
    return { answered: false, cancelled: false, deep: asked.deep, report };
  } catch (e) {
    if (signal?.aborted) return cancelled();
    throw e;
  }
}
