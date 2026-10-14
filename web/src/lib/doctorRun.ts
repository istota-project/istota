import { getAdminDoctor, requestDoctorRun, type DoctorReport } from '$lib/api';

// The daemon answers a request on its next tick (a second or so) and a deep
// run spawns a sandbox namespace, so this is generous; past it the daemon is
// not picking requests up, which is worth saying rather than spinning on.
export const POLL_MS = 1500;
export const WAIT_MS = 120_000;

export interface DoctorRunResult {
  /** Whether the daemon finished the request within `WAIT_MS`. */
  answered: boolean;
  /** Whether the request that was joined or made is a deep one. */
  deep: boolean;
  /** The last report read; null only if the wait ended before the first poll. */
  report: DoctorReport | null;
}

/**
 * Ask the daemon for a doctor run and poll `/admin/doctor` until it answers
 * that request or `WAIT_MS` passes. `onReport` sees every report read on the
 * way, so a page can show the latest one while it waits. Errors from either
 * call propagate.
 */
export async function waitForDoctorRun(
  deep: boolean,
  onReport?: (report: DoctorReport) => void,
): Promise<DoctorRunResult> {
  const asked = await requestDoctorRun(deep);
  let report: DoctorReport | null = null;
  const until = Date.now() + WAIT_MS;
  while (Date.now() < until) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    report = await getAdminDoctor();
    onReport?.(report);
    // The daemon's own marker, not a report's request_id: the next sweep
    // overwrites that, and a failed run still answers.
    if (report.answered?.id === asked.id) {
      return { answered: true, deep: asked.deep, report };
    }
  }
  return { answered: false, deep: asked.deep, report };
}
