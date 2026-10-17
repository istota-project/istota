import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { startPushBadgeSync } from './pushBadge';
import {
  notificationCounts,
  startNotificationPoll,
  resetNotifications,
  refreshCounts,
  applyNotificationCounts,
} from './notifications';

const native = vi.hoisted(() => ({
  pushAvailable: vi.fn(() => true),
  setBadge: vi.fn(async () => {}),
}));
vi.mock('$lib/platform/nativePush', () => native);
let count = { open: 5, actionable: 2, chat_unread: 9, push_badge: 3 };
let stop = () => {};
beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  count = { open: 5, actionable: 2, chat_unread: 9, push_badge: 3 };
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(count))),
  );
  resetNotifications();
});
afterEach(() => {
  stop();
  resetNotifications();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
const settle = () => vi.advanceTimersByTimeAsync(150);
it('waits for a known filtered total and preserves it across partial SSE frames', async () => {
  stop = startPushBadgeSync();
  await settle();
  expect(native.setBadge).not.toHaveBeenCalled();
  startNotificationPoll();
  await settle();
  expect(native.setBadge).toHaveBeenCalledWith(3);
  applyNotificationCounts({ open: 8, actionable: 2 });
  expect(get(notificationCounts).push_badge).toBe(3);
  await settle();
  expect(native.setBadge).toHaveBeenCalledTimes(1);
  await refreshCounts();
  await settle();
  expect(native.setBadge).toHaveBeenCalledTimes(1);
  count.push_badge = 0;
  await refreshCounts();
  await settle();
  expect(native.setBadge).toHaveBeenLastCalledWith(0);
  stop();
  count.push_badge = 7;
  await refreshCounts();
  await settle();
  expect(native.setBadge).toHaveBeenCalledTimes(2);
});
it('keeps the last badge offline and refreshes it on foreground', async () => {
  stop = startPushBadgeSync();
  startNotificationPoll();
  await settle();
  vi.mocked(fetch).mockRejectedValueOnce(new TypeError('offline'));
  await refreshCounts();
  await settle();
  expect(native.setBadge).toHaveBeenCalledTimes(1);
  count.push_badge = 1;
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
  await settle();
  expect(native.setBadge).toHaveBeenLastCalledWith(1);
});

it('reasserts the freshly fetched total on foreground even if it matches the last web write', async () => {
  stop = startPushBadgeSync();
  startNotificationPoll();
  await settle();
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
  await settle();
  expect(native.setBadge).toHaveBeenCalledTimes(2);
  expect(native.setBadge).toHaveBeenLastCalledWith(3);
});
it('ignores an older count response arriving after a read refresh', async () => {
  stop = startPushBadgeSync();
  startNotificationPoll();
  await settle();
  let resolve!: (value: Response) => void;
  vi.mocked(fetch).mockImplementationOnce(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const old = refreshCounts();
  count.push_badge = 0;
  await refreshCounts();
  resolve(new Response(JSON.stringify({ ...count, push_badge: 8 })));
  await old;
  await settle();
  expect(get(notificationCounts).push_badge).toBe(0);
  expect(native.setBadge).toHaveBeenLastCalledWith(0);
});

it('retries a failed native write when the same total is polled again', async () => {
  native.setBadge.mockRejectedValueOnce(new Error('Bridge unavailable'));
  stop = startPushBadgeSync();
  startNotificationPoll();
  await settle();
  await refreshCounts();
  await settle();
  expect(native.setBadge).toHaveBeenCalledTimes(2);
  expect(native.setBadge).toHaveBeenLastCalledWith(3);
});
it('serializes native writes and follows an in-flight write with the newest total', async () => {
  let resolve!: () => void;
  native.setBadge.mockImplementationOnce(
    () =>
      new Promise<void>((r) => {
        resolve = r;
      }),
  );
  stop = startPushBadgeSync();
  startNotificationPoll();
  await settle();
  count.push_badge = 1;
  await refreshCounts();
  await settle();
  expect(native.setBadge).toHaveBeenCalledTimes(1);
  resolve();
  await settle();
  expect(native.setBadge).toHaveBeenCalledTimes(2);
  expect(native.setBadge).toHaveBeenLastCalledWith(1);
});
