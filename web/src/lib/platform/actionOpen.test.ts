import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { startActionOpenHandling } from './actionOpen';
vi.mock('$app/paths', () => ({ base: '/istota' }));
const goto = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('$app/navigation', () => ({ goto }));
let pending: unknown = null;
let stop = () => {};
const consumePendingOpen = vi.fn(async () => {
  const open = pending;
  pending = null;
  return { open };
});
beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(navigator, 'userAgent', { value: 'IstotaApp/0.13.0', configurable: true });
  vi.stubGlobal('Capacitor', { Plugins: { IstotaActions: { consumePendingOpen } } });
});
afterEach(() => {
  stop();
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, 'userAgent', { value: 'Mozilla/5.0', configurable: true });
});

it('consumes once when mount and a warm event race, then removes its listener', async () => {
  pending = { kind: 'notes-settings' };
  stop = startActionOpenHandling();
  window.dispatchEvent(new Event('istotaActionOpen'));
  await vi.waitFor(() => expect(goto).toHaveBeenCalledTimes(1));
  stop();
  pending = { kind: 'notes-settings' };
  window.dispatchEvent(new Event('istotaActionOpen'));
  await Promise.resolve();
  expect(goto).toHaveBeenCalledTimes(1);
});

it('opens notes settings at the quick notes card', async () => {
  pending = { kind: 'notes-settings' };
  stop = startActionOpenHandling();
  await vi.waitFor(() =>
    expect(goto).toHaveBeenCalledWith('/istota/settings/preferences/#quick-notes'),
  );
});

it('opens the folder a saved note is in', async () => {
  pending = { kind: 'files', path: 'Notes/Inbox/Buy milk.md' };
  stop = startActionOpenHandling();
  await vi.waitFor(() =>
    expect(goto).toHaveBeenCalledWith('/istota/files?place=home&path=Notes%2FInbox'),
  );
});

it('opens a folder path as it is', async () => {
  pending = { kind: 'files', path: 'Notes/Inbox' };
  stop = startActionOpenHandling();
  await vi.waitFor(() =>
    expect(goto).toHaveBeenCalledWith('/istota/files?place=home&path=Notes%2FInbox'),
  );
});

it.each([
  ['a parent part', '../secret/x.md'],
  ['a control character', 'Notes/\nInbox'],
  ['no path', undefined],
])('opens the root of My files for %s', async (_label, path) => {
  pending = { kind: 'files', path };
  stop = startActionOpenHandling();
  await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/istota/files?place=home'));
});

it("opens the voice sheet's room through the server deep link", async () => {
  pending = { kind: 'room', token: 'abc_D-9' };
  const navigate = vi.fn();
  stop = startActionOpenHandling(navigate);
  await vi.waitFor(() => expect(navigate).toHaveBeenCalledWith('/istota/chat/r/abc_D-9'));
  expect(goto).not.toHaveBeenCalled();
});

it.each([
  ['a token with a path separator', 'rm_x/t/5'],
  ['an empty token', ''],
  ['no token', undefined],
])('opens the chat page default for a room open with %s', async (_label, token) => {
  pending = { kind: 'room', token };
  const navigate = vi.fn();
  stop = startActionOpenHandling(navigate);
  await vi.waitFor(() => expect(goto).toHaveBeenCalledWith('/istota/chat'));
  expect(navigate).not.toHaveBeenCalled();
});

it('ignores an open of a kind it does not know', async () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  pending = { kind: 'voice' };
  stop = startActionOpenHandling();
  await vi.waitFor(() => expect(consumePendingOpen).toHaveBeenCalled());
  await Promise.resolve();
  expect(goto).not.toHaveBeenCalled();
  warn.mockRestore();
});

it('is inert on a shell without the plugin version', async () => {
  Object.defineProperty(navigator, 'userAgent', { value: 'IstotaApp/0.12.0', configurable: true });
  pending = { kind: 'notes-settings' };
  stop = startActionOpenHandling();
  window.dispatchEvent(new Event('istotaActionOpen'));
  await Promise.resolve();
  expect(consumePendingOpen).not.toHaveBeenCalled();
  expect(goto).not.toHaveBeenCalled();
});

it('does not navigate after teardown while consume is pending', async () => {
  let resolve!: (value: unknown) => void;
  consumePendingOpen.mockImplementationOnce(
    () =>
      new Promise((r) => {
        resolve = r as (value: unknown) => void;
      }),
  );
  stop = startActionOpenHandling();
  stop();
  resolve({ open: { kind: 'notes-settings' } });
  await Promise.resolve();
  await Promise.resolve();
  expect(goto).not.toHaveBeenCalled();
});
