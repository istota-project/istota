import { get, writable, type Readable } from 'svelte/store';
import { AuthError, getNavUnreadCounts, type NavUnreadCounts } from '$lib/api';
import { online } from './connectivity';
import { latest } from '@minot/ui/ui/latest';

type Counts = Omit<NavUnreadCounts, 'enabled'>;
const empty: Counts = { chat: null, briefings: null, feeds: null };
const counts = writable<Counts>(empty);
export const navUnreadCounts: Readable<Counts> = { subscribe: counts.subscribe };

let username: string | null = null;
// `sessions` ends with each stop; `revisions` with anything that makes an
// answer already on the wire out of date (availability, a mutation).
const sessions = latest();
const revisions = latest();
let dirty = false;
let available = false;
let inFlight = false;
let failures = 0;
let lastRequest = -Infinity;
let timer: ReturnType<typeof setTimeout> | null = null;
let unwatch: (() => void) | null = null;

function cancelTimer() {
  if (timer !== null) clearTimeout(timer);
  timer = null;
}

function schedule(delay: number) {
  cancelTimer();
  if (!username || !available || inFlight) return;
  timer = setTimeout(() => {
    timer = null;
    void refresh();
  }, delay);
}

function validSummary(value: NavUnreadCounts): boolean {
  if (!value || typeof value.enabled !== 'boolean') return false;
  return [value.chat, value.briefings, value.feeds].every(
    (count) => count === null || (Number.isSafeInteger(count) && Number(count) >= 0),
  );
}

async function refresh() {
  if (!username || !available || inFlight) return;
  cancelTimer();
  inFlight = true;
  dirty = false;
  lastRequest = Date.now();
  const sameSession = sessions.peek();
  const unchanged = revisions.peek();
  try {
    const next = await getNavUnreadCounts();
    if (!sameSession()) return;
    if (!validSummary(next)) throw new Error('Invalid navigation counts');
    if (!next.enabled) {
      stopNavUnreadPoll();
      return;
    }
    if (!unchanged() || !available) return;
    counts.set({ chat: next.chat, briefings: next.briefings, feeds: next.feeds });
    failures = 0;
  } catch (error) {
    if (!sameSession()) return;
    if (error instanceof AuthError) {
      stopNavUnreadPoll();
      return;
    }
    counts.set(empty);
    failures += 1;
  } finally {
    // Keep the slot across stop/start, so an old account request cannot overlap
    // the new one's. Its answer is discarded, then the new lifecycle runs.
    inFlight = false;
    if (dirty) schedule(Math.max(0, lastRequest + 1000 - Date.now()));
    else schedule(failures >= 5 ? 300_000 : 30_000);
  }
}

function updateAvailability() {
  const next = get(online) && document.visibilityState === 'visible';
  if (next === available) return;
  available = next;
  revisions.invalidate();
  dirty = true;
  cancelTimer();
  counts.set(empty);
  if (available) {
    failures = 0;
    // A return to the app is a fresh observation, even during backoff.
    lastRequest = -Infinity;
    void refresh();
  }
}

export function startNavUnreadPoll(user: string) {
  if (username === user) return;
  stopNavUnreadPoll();
  username = user;
  dirty = true;
  document.addEventListener('visibilitychange', updateAvailability);
  unwatch = online.subscribe(updateAvailability);
}

export function stopNavUnreadPoll() {
  sessions.invalidate();
  username = null;
  available = false;
  dirty = false;
  failures = 0;
  lastRequest = -Infinity;
  cancelTimer();
  unwatch?.();
  unwatch = null;
  if (typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', updateAvailability);
  }
  counts.set(empty);
}

export function invalidateNavUnreadCounts() {
  if (!username) return;
  revisions.invalidate();
  dirty = true;
  schedule(Math.max(0, lastRequest + 1000 - Date.now()));
}
