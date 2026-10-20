import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  actionsAvailable,
  actionsStatus,
  consumeActionOpen,
  forgetActions,
  onActionOpen,
  provisionActions,
} from './nativeActions';

const STATUS = {
  deviceId: '6F9619FF-8B86-D011-B42D-00C04FC964FF',
  scopes: ['notes'],
  tokenRejected: false,
  pendingNotes: 2,
  failedNotes: 1,
};
const plugin = {
  status: vi.fn(async () => STATUS),
  provision: vi.fn(async () => ({})),
  forget: vi.fn(async () => ({})),
  consumePendingOpen: vi.fn(async () => ({ open: { kind: 'notes-settings' } })),
};
function shell(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('Capacitor', { Plugins: { IstotaActions: plugin } });
  shell('IstotaApp/0.13.0');
});
afterEach(() => {
  vi.unstubAllGlobals();
  shell('Mozilla/5.0');
});

it.each(['Mozilla/5.0', 'IstotaApp/0.12.0'])('is entirely inert for %s', async (ua) => {
  shell(ua);
  expect(actionsAvailable()).toBe(false);
  expect(await actionsStatus()).toBeNull();
  expect(await consumeActionOpen()).toBeNull();
  await provisionActions('secret', ['notes'], 'Notes/Inbox');
  await forgetActions();
  const handler = vi.fn();
  const stop = onActionOpen(handler);
  window.dispatchEvent(new Event('istotaActionOpen'));
  stop();
  expect(handler).not.toHaveBeenCalled();
  for (const fn of Object.values(plugin)) expect(fn).not.toHaveBeenCalled();
});

it('degrades when the plugin is absent', async () => {
  vi.stubGlobal('Capacitor', { Plugins: {} });
  expect(actionsAvailable()).toBe(false);
  expect(await actionsStatus()).toBeNull();
  expect(await consumeActionOpen()).toBeNull();
  await provisionActions(null, ['notes'], null);
});

it('uses the native contract on 0.13.0 and ignores event payloads', async () => {
  expect(actionsAvailable()).toBe(true);
  expect(await actionsStatus()).toEqual(STATUS);
  await provisionActions('secret', ['notes'], 'Notes/Inbox');
  expect(plugin.provision).toHaveBeenCalledWith({
    token: 'secret',
    scopes: ['notes'],
    notesFolder: 'Notes/Inbox',
  });
  await provisionActions(null, ['notes'], 'Notes/Other');
  expect(plugin.provision).toHaveBeenLastCalledWith({
    token: null,
    scopes: ['notes'],
    notesFolder: 'Notes/Other',
  });
  await forgetActions();
  expect(plugin.forget).toHaveBeenCalledTimes(1);
  expect(await consumeActionOpen()).toEqual({ kind: 'notes-settings' });
  const handler = vi.fn();
  const stop = onActionOpen(handler);
  window.dispatchEvent(
    new CustomEvent('istotaActionOpen', { detail: { kind: 'files', path: 'x' } }),
  );
  expect(handler).toHaveBeenCalledWith();
  stop();
  window.dispatchEvent(new Event('istotaActionOpen'));
  expect(handler).toHaveBeenCalledTimes(1);
});

it('gives the empty answer when the plugin rejects', async () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  plugin.status.mockRejectedValueOnce(new Error('CONNECTION_CHANGED'));
  plugin.consumePendingOpen.mockRejectedValueOnce(new Error('CONNECTION_CHANGED'));
  plugin.provision.mockRejectedValueOnce(new Error('STORE_FAILED'));
  plugin.forget.mockRejectedValueOnce(new Error('STORE_FAILED'));
  expect(await actionsStatus()).toBeNull();
  expect(await consumeActionOpen()).toBeNull();
  await expect(provisionActions('secret', ['notes'], null)).resolves.toBeUndefined();
  await expect(forgetActions()).resolves.toBeUndefined();
  warn.mockRestore();
});
