/**
 * The escape hatch behind settings → "Clear offline data" (ISSUE-202).
 *
 * Everything this feature stores on the device, dropped in one go: the service
 * worker and the app shell it precached, and the IndexedDB cache of rooms,
 * transcripts, config and held attachment bytes. It exists because the offline
 * machinery is the one part of this app whose failures a reload cannot fix — a
 * worker serving a document from a build the server has deleted, a cache that
 * will not reconcile — and the alternative on a phone is deleting the app.
 *
 * What it deliberately does **not** touch is the send queue in `localStorage`.
 * A queued message is text the user committed to sending, and it is not
 * offline *data* in the sense the button offers to clear; it survives, and goes
 * out on the next drain. The files held with those messages do not — they live
 * in the cache being cleared — which is why the confirmation says so and why
 * the queue's own error path already covers a message whose bytes are gone.
 *
 * Every step is independently best-effort. A browser with no Cache Storage, a
 * worker that was never registered, a database that cannot be opened: none of
 * them should stop the rest, because a half-cleared state is exactly what the
 * caller pressed the button to get out of.
 */
import { base } from '$app/paths';
import { clearOffline } from './db';

/** What each of the three steps managed. The caller reports the last one. */
export interface ClearOfflineResult {
  workers: number;
  caches: number;
  /** False when the stored data is still there — see `clearOffline`. */
  database: boolean;
}

/**
 * This app's own registrations, and not the origin's.
 *
 * `getRegistrations()` answers with every worker on the origin, and this app is
 * served under a base path on a host that may carry other things — the
 * deployment's Nextcloud among them. A row offering to clear *this app's*
 * offline data must not unregister a neighbour's worker, which is the same
 * rule the worker's own activate step follows for caches.
 */
function isOurs(registration: ServiceWorkerRegistration): boolean {
  try {
    return new URL(registration.scope).pathname.startsWith(`${base}/`);
  } catch {
    return false;
  }
}

async function unregisterWorkers(): Promise<number> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return 0;
  try {
    const registrations = (await navigator.serviceWorker.getRegistrations()).filter(isOurs);
    const results = await Promise.all(
      registrations.map((registration) => registration.unregister().catch(() => false)),
    );
    return results.filter(Boolean).length;
  } catch {
    return 0;
  }
}

async function deleteCaches(): Promise<number> {
  if (typeof caches === 'undefined') return 0;
  try {
    const keys = await caches.keys();
    // This app's own generations only, the same rule the worker's activate
    // step follows: deleting storage we did not write is not ours to offer.
    const ours = keys.filter((key) => key.startsWith('istota-'));
    const results = await Promise.all(ours.map((key) => caches.delete(key).catch(() => false)));
    return results.filter(Boolean).length;
  } catch {
    return 0;
  }
}

export async function clearOfflineData(): Promise<ClearOfflineResult> {
  const workers = await unregisterWorkers();
  const cacheCount = await deleteCaches();
  let database = false;
  try {
    // The one step that reports rather than swallows: an aborted or timed-out
    // transaction leaves the transcripts and the held bytes where they were,
    // and the caller reloads over the top, so it has to be able to say that
    // the button did nothing.
    database = await clearOffline();
  } catch {
    database = false;
  }
  return { workers, caches: cacheCount, database };
}
