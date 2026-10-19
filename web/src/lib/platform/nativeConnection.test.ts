import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  appConnectionAvailable,
  connectionContext,
  openAppSettings,
  recordPushRegistration,
  signOutOfApp,
} from './nativeConnection';

const context = {
  connectionBinding: '0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51',
  generation: 3,
  baseURL: 'https://your-host.tld/istota/',
  state: 'active' as const,
};
const plugin = {
  current: vi.fn(async () => context),
  recordPushRegistration: vi.fn(async () => {}),
  openSettings: vi.fn(async () => {}),
  signOut: vi.fn(async (): Promise<void> => {}),
};
function shell(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('Capacitor', { Plugins: { IstotaConnection: plugin } });
  shell('IstotaApp/0.12.0');
});
afterEach(() => {
  vi.unstubAllGlobals();
  shell('Mozilla/5.0');
});

it.each(['Mozilla/5.0', 'IstotaApp/0.11.0'])('is inert for %s', async (ua) => {
  shell(ua);
  expect(await connectionContext()).toBeNull();
  await recordPushRegistration(7, context);
  expect(plugin.current).not.toHaveBeenCalled();
  expect(plugin.recordPushRegistration).not.toHaveBeenCalled();
});

it('reads the context and records a registration for that context', async () => {
  expect(await connectionContext()).toEqual(context);
  await recordPushRegistration(7, context);
  expect(plugin.recordPushRegistration).toHaveBeenCalledWith({
    registrationId: 7,
    connectionBinding: context.connectionBinding,
    generation: 3,
  });
});

it('passes a changed connection through as a failure', async () => {
  plugin.current.mockRejectedValueOnce(new Error('The connection changed.'));
  await expect(connectionContext()).rejects.toThrow('The connection changed.');
  plugin.recordPushRegistration.mockRejectedValueOnce(new Error('The connection changed.'));
  await expect(recordPushRegistration(7, context)).rejects.toThrow('The connection changed.');
});

describe('app settings and sign-out', () => {
  it.each(['Mozilla/5.0', 'IstotaApp/0.11.0'])('offers neither in %s', async (ua) => {
    shell(ua);
    expect(appConnectionAvailable()).toBe(false);
    expect(await openAppSettings()).toBe(false);
    expect(await signOutOfApp()).toBe('unavailable');
    expect(plugin.openSettings).not.toHaveBeenCalled();
    expect(plugin.signOut).not.toHaveBeenCalled();
  });

  it('opens the native settings', async () => {
    expect(appConnectionAvailable()).toBe(true);
    expect(await openAppSettings()).toBe(true);
    expect(plugin.openSettings).toHaveBeenCalledOnce();
  });

  it('reports a confirmed sign-out', async () => {
    expect(await signOutOfApp()).toBe('signed-out');
  });

  it.each(['USER_CANCELLED', 'SIGN_OUT_PENDING'])(
    'treats %s as the user saying no',
    async (code) => {
      plugin.signOut.mockRejectedValueOnce(Object.assign(new Error('no'), { code }));
      expect(await signOutOfApp()).toBe('cancelled');
    },
  );

  it('passes any other refusal through', async () => {
    plugin.signOut.mockRejectedValueOnce(
      Object.assign(new Error('The connection changed.'), { code: 'CONNECTION_CHANGED' }),
    );
    await expect(signOutOfApp()).rejects.toThrow('The connection changed.');
  });
});
