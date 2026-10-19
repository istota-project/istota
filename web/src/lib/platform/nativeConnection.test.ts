import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { connectionContext, recordPushRegistration } from './nativeConnection';

const context = {
  connectionBinding: '0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51',
  generation: 3,
  baseURL: 'https://your-host.tld/istota/',
  state: 'active' as const,
};
const plugin = {
  current: vi.fn(async () => context),
  recordPushRegistration: vi.fn(async () => {}),
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
