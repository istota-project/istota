import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  pushAvailable,
  pushPermissions,
  registerForPush,
  setBadge,
  consumePendingOpen,
  onPushOpen,
  privatePushAvailable,
  preparePrivateRegistration,
  commitPrivateRegistration,
  cancelPrivateRegistration,
} from './nativePush';

const plugin = {
  permissions: vi.fn(),
  register: vi.fn(),
  setBadge: vi.fn(),
  consumePendingOpen: vi.fn(),
};
function shell(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('Capacitor', { Plugins: { IstotaPush: plugin } });
  shell('IstotaApp/0.11.0');
});
afterEach(() => {
  vi.unstubAllGlobals();
  shell('Mozilla/5.0');
});

it.each(['Mozilla/5.0', 'IstotaApp/0.10.0'])('is entirely inert for %s', async (ua) => {
  shell(ua);
  expect(pushAvailable()).toBe(false);
  expect(await pushPermissions()).toBeNull();
  expect(await registerForPush()).toBeNull();
  expect(await consumePendingOpen()).toBeNull();
  await setBadge(4);
  const handler = vi.fn();
  const stop = onPushOpen(handler);
  window.dispatchEvent(new Event('istotaPushOpen'));
  stop();
  expect(handler).not.toHaveBeenCalled();
  for (const fn of Object.values(plugin)) expect(fn).not.toHaveBeenCalled();
});
it('degrades when the plugin is absent', async () => {
  vi.stubGlobal('Capacitor', { Plugins: {} });
  expect(pushAvailable()).toBe(false);
  expect(await pushPermissions()).toBeNull();
  expect(await registerForPush()).toBeNull();
  expect(await consumePendingOpen()).toBeNull();
  await setBadge(2);
});
it('uses the native contract and ignores event payloads', async () => {
  plugin.permissions.mockResolvedValue({ permission: 'prompt', registered: false });
  plugin.register.mockResolvedValue({
    permission: 'granted',
    token: 'ab'.repeat(32),
    environment: 'sandbox',
  });
  plugin.consumePendingOpen.mockResolvedValue({ open: { kind: 'chat', room_id: 12 } });
  expect(pushAvailable()).toBe(true);
  expect(await pushPermissions()).toEqual({ permission: 'prompt', registered: false });
  expect(await registerForPush()).toMatchObject({ environment: 'sandbox' });
  expect(await consumePendingOpen()).toEqual({ kind: 'chat', room_id: 12 });
  await setBadge(8);
  expect(plugin.setBadge).toHaveBeenCalledWith({ count: 8 });
  const handler = vi.fn();
  const stop = onPushOpen(handler);
  window.dispatchEvent(
    new CustomEvent('istotaPushOpen', { detail: { kind: 'chat', room_id: 99 } }),
  );
  expect(handler).toHaveBeenCalledWith();
  stop();
  window.dispatchEvent(new Event('istotaPushOpen'));
  expect(handler).toHaveBeenCalledTimes(1);
});
it('preserves denial and actual registration failures', async () => {
  plugin.register.mockResolvedValueOnce({
    permission: 'denied',
    token: null,
    environment: 'sandbox',
  });
  expect(await registerForPush()).toMatchObject({ permission: 'denied', token: null });
  plugin.register.mockRejectedValueOnce(new Error('PUSH_TOKEN_TIMEOUT'));
  await expect(registerForPush()).rejects.toThrow('PUSH_TOKEN_TIMEOUT');
});

const privatePlugin = {
  ...plugin,
  preparePrivateRegistration: vi.fn(),
  commitPrivateRegistration: vi.fn(),
  cancelPrivateRegistration: vi.fn(),
};
const privateBinding = '0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51';

it.each(['Mozilla/5.0', 'IstotaApp/0.14.0'])('keeps private enrollment off for %s', async (ua) => {
  shell(ua);
  vi.stubGlobal('Capacitor', { Plugins: { IstotaPush: privatePlugin } });
  expect(privatePushAvailable()).toBe(false);
  expect(
    await preparePrivateRegistration({ binding: privateBinding, senderKey: 'k', senderKeyId: 'i' }),
  ).toBeNull();
  await commitPrivateRegistration({ binding: privateBinding, generation: 'g', deviceId: 4 });
  await cancelPrivateRegistration({ binding: privateBinding, generation: 'g' });
  expect(privatePlugin.preparePrivateRegistration).not.toHaveBeenCalled();
  expect(privatePlugin.commitPrivateRegistration).not.toHaveBeenCalled();
  expect(privatePlugin.cancelPrivateRegistration).not.toHaveBeenCalled();
});

it('keeps private enrollment off on a 0.15.0 shell whose plugin lacks the methods', () => {
  shell('IstotaApp/0.15.0');
  expect(pushAvailable()).toBe(true);
  expect(privatePushAvailable()).toBe(false);
});

it('passes the three private calls through on a 0.15.0 shell', async () => {
  shell('IstotaApp/0.15.0');
  vi.stubGlobal('Capacitor', { Plugins: { IstotaPush: privatePlugin } });
  const prepared = {
    content: 'generic',
    generation: 'A'.repeat(22),
    token: 'ab'.repeat(32),
    environment: 'sandbox',
    versions: [1],
  };
  privatePlugin.preparePrivateRegistration.mockResolvedValue(prepared);
  privatePlugin.commitPrivateRegistration.mockResolvedValue({});
  privatePlugin.cancelPrivateRegistration.mockResolvedValue({});
  expect(privatePushAvailable()).toBe(true);
  const input = { binding: privateBinding, senderKey: 'k', senderKeyId: 'i' };
  expect(await preparePrivateRegistration(input)).toEqual(prepared);
  expect(privatePlugin.preparePrivateRegistration).toHaveBeenCalledWith(input);
  await commitPrivateRegistration({ binding: privateBinding, generation: 'g', deviceId: 4 });
  expect(privatePlugin.commitPrivateRegistration).toHaveBeenCalledWith({
    binding: privateBinding,
    generation: 'g',
    deviceId: 4,
  });
  await cancelPrivateRegistration({ binding: privateBinding, generation: 'g' });
  expect(privatePlugin.cancelPrivateRegistration).toHaveBeenCalledWith({
    binding: privateBinding,
    generation: 'g',
  });
});
