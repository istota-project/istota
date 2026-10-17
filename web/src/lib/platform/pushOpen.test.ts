import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { startPushOpenHandling } from './pushOpen';
vi.mock('$app/paths', () => ({ base: '/istota' }));
const goto = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('$app/navigation', () => ({ goto }));
let pending: unknown = null;
let stop = () => {};
beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(navigator, 'userAgent', { value: 'IstotaApp/0.11.0', configurable: true });
  vi.stubGlobal('Capacitor', {
    Plugins: {
      IstotaPush: {
        consumePendingOpen: vi.fn(async () => {
          const open = pending;
          pending = null;
          return { open };
        }),
      },
    },
  });
});
afterEach(() => {
  stop();
  vi.unstubAllGlobals();
});
it('consumes once when mount and a warm event race, then removes its listener', async () => {
  pending = { kind: 'chat', room_id: 12 };
  stop = startPushOpenHandling();
  window.dispatchEvent(
    new CustomEvent('istotaPushOpen', { detail: { kind: 'chat', room_id: 99 } }),
  );
  await vi.waitFor(() => expect(goto).toHaveBeenCalledTimes(1));
  expect(goto).toHaveBeenCalledWith('/istota/chat?room=12');
  stop();
  pending = { kind: 'chat', room_id: 13 };
  window.dispatchEvent(new Event('istotaPushOpen'));
  await Promise.resolve();
  expect(goto).toHaveBeenCalledTimes(1);
});
it.each([
  'https://evil.example/chat',
  '//evil.example/chat',
  '/chat/../logout',
  '/chat%2flogout',
  '/chat?room=1',
  '/chat\n',
])('falls back for unsafe links: %s', async (link) => {
  pending = { kind: 'notification', link };
  stop = startPushOpenHandling();
  await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/istota/chat'));
});
it('opens a validated notification route under the deployment base', async () => {
  pending = { kind: 'notification', link: '/settings/notifications' };
  stop = startPushOpenHandling();
  await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/istota/settings/notifications'));
});
it('uses full navigation for server redirect routes', async () => {
  pending = { kind: 'notification', link: '/chat/r/room-one' };
  const navigate = vi.fn();
  stop = startPushOpenHandling(navigate);
  await vi.waitFor(() => expect(navigate).toHaveBeenCalledWith('/istota/chat/r/room-one'));
  expect(goto).not.toHaveBeenCalled();
});
it('does not navigate after teardown while consume is pending', async () => {
  let resolve!: (value: unknown) => void;
  vi.stubGlobal('Capacitor', {
    Plugins: {
      IstotaPush: {
        consumePendingOpen: () =>
          new Promise((r) => {
            resolve = r;
          }),
      },
    },
  });
  stop = startPushOpenHandling();
  stop();
  resolve({ open: { kind: 'chat', room_id: 12 } });
  await Promise.resolve();
  await Promise.resolve();
  expect(goto).not.toHaveBeenCalled();
});
