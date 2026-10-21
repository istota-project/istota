import { pushAvailable, setBadge } from '$lib/platform/nativePush';
import { notificationCounts } from './notifications';
import { onDocumentVisible } from '@minot/ui/ui/visibility';

/** Subscribe after authentication; an absent total is unknown, never zero. */
export function startPushBadgeSync(): () => void {
  if (!pushAvailable()) return () => {};
  let stopped = false;
  let last: number | undefined;
  let wanted: number | undefined;
  let writing = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function flush() {
    timer = undefined;
    if (stopped || writing || wanted === undefined || wanted === last) return;
    writing = true;
    const count = wanted;
    try {
      await setBadge(count);
      last = count;
    } catch {
      // Retry on the next count poll, without claiming the write succeeded.
    } finally {
      writing = false;
      if (!stopped && wanted !== count) schedule();
    }
  }
  function schedule() {
    if (timer !== undefined) clearTimeout(timer);
    timer = setTimeout(() => void flush(), 100);
  }
  const unsubscribe = notificationCounts.subscribe((counts) => {
    const count = counts.push_badge;
    if (typeof count !== 'number' || !Number.isFinite(count) || count < 0) {
      wanted = undefined;
      if (timer !== undefined) clearTimeout(timer);
      return;
    }
    wanted = Math.min(9999, Math.trunc(count));
    if (wanted !== last) schedule();
  });
  // APNs can change the icon while suspended. Wait for the poll's fresh answer
  // before reasserting it, even when it equals our last successful write.
  const offVisible = onDocumentVisible(() => {
    last = undefined;
  });
  return () => {
    offVisible();
    stopped = true;
    unsubscribe();
    if (timer !== undefined) clearTimeout(timer);
  };
}
