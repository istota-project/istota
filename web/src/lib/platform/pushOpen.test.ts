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
  pending = { kind: 'chat', room_token: 'room-one', room_id: 12 };
  const navigate = vi.fn();
  stop = startPushOpenHandling(navigate);
  window.dispatchEvent(
    new CustomEvent('istotaPushOpen', { detail: { kind: 'chat', room_token: 'room-two' } }),
  );
  await vi.waitFor(() => expect(navigate).toHaveBeenCalledTimes(1));
  stop();
  pending = { kind: 'chat', room_token: 'room-three' };
  window.dispatchEvent(new Event('istotaPushOpen'));
  await Promise.resolve();
  expect(navigate).toHaveBeenCalledTimes(1);
});
it('opens a chat tap through the room deep link, by token and not the numeric handle', async () => {
  // The chat page reads `room=` as a token; the deep link also loads a fresh room list.
  pending = { kind: 'chat', room_token: 'abc_D-9', room_id: 12 };
  const navigate = vi.fn();
  stop = startPushOpenHandling(navigate);
  await vi.waitFor(() => expect(navigate).toHaveBeenCalledWith('/istota/chat/r/abc_D-9'));
  expect(goto).not.toHaveBeenCalled();
});
it.each([
  ['no token', { kind: 'chat', room_id: 12 }],
  ['a token with a path separator', { kind: 'chat', room_token: 'rm_x/t/5' }],
  ['an empty token', { kind: 'chat', room_token: '' }],
  ['a token that is not a string', { kind: 'chat', room_token: 7 }],
])('falls back to the chat page for %s', async (_label, open) => {
  pending = open;
  const navigate = vi.fn();
  stop = startPushOpenHandling(navigate);
  await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/istota/chat'));
  expect(navigate).not.toHaveBeenCalled();
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
  resolve({ open: { kind: 'chat', room_token: 'room-one' } });
  await Promise.resolve();
  await Promise.resolve();
  expect(goto).not.toHaveBeenCalled();
});
