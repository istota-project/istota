import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { refreshStaleEnrollment } from './pushEnrollment';
vi.mock('$app/paths', () => ({ base: '/istota' }));

const binding = '0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51';
const token = 'ab'.repeat(31) + 'c0ffee';
const push = {
  permissions: vi.fn(async () => ({ permission: 'granted', registered: false })),
  register: vi.fn(async () => ({ permission: 'granted', token, environment: 'sandbox' })),
};
const connection = {
  current: vi.fn(async () => ({
    connectionBinding: binding,
    generation: 3,
    baseURL: 'https://your-host.tld/istota/',
    state: 'active',
  })),
  recordPushRegistration: vi.fn(async () => {}),
};
let devices: Record<string, unknown>[] = [];
const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  if (url === '/istota/api/push/devices' && init?.method === 'POST') {
    return new Response(JSON.stringify({ id: 41 }));
  }
  return new Response(JSON.stringify({ devices }));
});
function device(overrides: Record<string, unknown> = {}) {
  return {
    id: 5,
    platform: 'ios',
    app_version: '0.11.2',
    environment: 'sandbox',
    token_tail: 'c0ffee',
    created_at: '2026-10-01T00:00:00Z',
    last_seen_at: null,
    last_error: null,
    ...overrides,
  };
}
function shell(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}
function posts() {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
}
beforeEach(() => {
  vi.clearAllMocks();
  devices = [device()];
  shell('IstotaApp/0.12.0');
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('Capacitor', { Plugins: { IstotaPush: push, IstotaConnection: connection } });
});
afterEach(() => {
  vi.unstubAllGlobals();
  shell('Mozilla/5.0');
});

it('re-enrolls a row left by an older app with this connection and records the receipt', async () => {
  expect(await refreshStaleEnrollment()).toBe(true);
  expect(posts()).toHaveLength(1);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({
    token,
    environment: 'sandbox',
    platform: 'ios',
    app_version: '0.12.0',
    connection_binding: binding,
    connection_generation: 3,
  });
  expect(connection.recordPushRegistration).toHaveBeenCalledWith({
    registrationId: 41,
    connectionBinding: binding,
    generation: 3,
  });
});

it('does nothing when the row already carries the running version', async () => {
  devices = [device({ app_version: '0.12.0' })];
  expect(await refreshStaleEnrollment()).toBe(false);
  expect(push.register).not.toHaveBeenCalled();
  expect(posts()).toHaveLength(0);
});

it.each([
  ['another token', { token_tail: '123456' }],
  ['another environment', { environment: 'production' }],
])('does not enroll this device when the stale row is %s', async (_label, overrides) => {
  devices = [device(overrides)];
  expect(await refreshStaleEnrollment()).toBe(false);
  expect(posts()).toHaveLength(0);
});

it.each(['prompt', 'denied'])('never asks or enrolls when permission is %s', async (permission) => {
  push.permissions.mockResolvedValueOnce({ permission, registered: false });
  expect(await refreshStaleEnrollment()).toBe(false);
  expect(push.register).not.toHaveBeenCalled();
  expect(fetchMock).not.toHaveBeenCalled();
});

it('is inert on a shell without the connection plugin', async () => {
  shell('IstotaApp/0.11.2');
  expect(await refreshStaleEnrollment()).toBe(false);
  expect(push.permissions).not.toHaveBeenCalled();
  expect(fetchMock).not.toHaveBeenCalled();
});

it('swallows a failure so the page load is unaffected', async () => {
  push.register.mockRejectedValueOnce(new Error('PUSH_TOKEN_TIMEOUT'));
  expect(await refreshStaleEnrollment()).toBe(false);
  expect(posts()).toHaveLength(0);
});
