import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { refreshStaleEnrollment, saveEnrollment } from './pushEnrollment';
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
const privateDiscovery = { push: { delivery: 'direct', private: { versions: [1] } } };
let discovery: { status: number; body: unknown } = { status: 200, body: privateDiscovery };
const senderKey = { version: 1, key_id: 'S'.repeat(43), public_key: 'P'.repeat(43) };
let senderKeys: (typeof senderKey)[] = [];
let postReplies: { status: number; body: unknown }[] = [];
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}
const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  if (url === '/istota/api/push/devices' && init?.method === 'POST') {
    const reply = postReplies.shift() ?? { status: 200, body: { id: 41 } };
    return json(reply.body, reply.status);
  }
  if (url === '/istota/api/mobile/config') return json(discovery.body, discovery.status);
  if (url === '/istota/api/push/sender-key') return json(senderKeys.shift() ?? senderKey);
  if (init?.method === 'DELETE') return json({ ok: true });
  return json({ devices });
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
  discovery = { status: 200, body: privateDiscovery };
  senderKeys = [];
  postReplies = [];
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

// Private enrollment (0.15.0): sender key, native prepare, POST with `private`, native commit.
const preparedToken = 'cd'.repeat(32);
function prepared(overrides: Record<string, unknown> = {}) {
  return {
    content: 'encrypted',
    encryptionPublicKey: 'E'.repeat(43),
    encryptionKeyId: 'F'.repeat(43),
    generation: 'G'.repeat(22),
    token: preparedToken,
    environment: 'production',
    versions: [1],
    ...overrides,
  };
}
const privatePush = {
  ...push,
  preparePrivateRegistration: vi.fn(async (_input: unknown) => prepared()),
  commitPrivateRegistration: vi.fn(async (_input: unknown) => ({})),
  cancelPrivateRegistration: vi.fn(async (_input: unknown) => ({})),
};
const context = {
  connectionBinding: binding,
  generation: 3,
  baseURL: 'https://your-host.tld/istota/',
  state: 'active' as const,
};
const registration = { permission: 'granted' as const, token, environment: 'sandbox' as const };
function onPrivateShell(version = '0.15.0') {
  shell(`IstotaApp/${version}`);
  vi.stubGlobal('Capacitor', {
    Plugins: { IstotaPush: privatePush, IstotaConnection: connection },
  });
}
function postedBodies() {
  return posts().map(([, init]) => JSON.parse(init!.body as string));
}
function urls() {
  return fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${url}`);
}
const legacyBody = (version: string) => ({
  token,
  environment: 'sandbox',
  platform: 'ios',
  app_version: version,
  connection_binding: binding,
  connection_generation: 3,
});

it('enrolls privately in five steps and commits before recording the receipt', async () => {
  onPrivateShell();
  await saveEnrollment(registration, context);
  expect(urls()).toEqual([
    'GET /istota/api/mobile/config',
    'GET /istota/api/push/sender-key',
    'POST /istota/api/push/devices',
  ]);
  expect(privatePush.preparePrivateRegistration).toHaveBeenCalledWith({
    binding,
    senderKey: senderKey.public_key,
    senderKeyId: senderKey.key_id,
  });
  expect(postedBodies()).toEqual([
    {
      token: preparedToken,
      environment: 'production',
      platform: 'ios',
      app_version: '0.15.0',
      connection_binding: binding,
      connection_generation: 3,
      private: {
        version: 1,
        content: 'encrypted',
        encryption_public_key: 'E'.repeat(43),
        encryption_key_id: 'F'.repeat(43),
        sender_key_id: senderKey.key_id,
        generation: 'G'.repeat(22),
      },
    },
  ]);
  expect(privatePush.commitPrivateRegistration).toHaveBeenCalledWith({
    binding,
    generation: 'G'.repeat(22),
    deviceId: 41,
  });
  expect(connection.recordPushRegistration).toHaveBeenCalledWith({
    registrationId: 41,
    connectionBinding: binding,
    generation: 3,
  });
  expect(privatePush.commitPrivateRegistration.mock.invocationCallOrder[0]).toBeLessThan(
    connection.recordPushRegistration.mock.invocationCallOrder[0],
  );
  expect(privatePush.cancelPrivateRegistration).not.toHaveBeenCalled();
});

it('sends only the version, content and generation for a generic enrollment', async () => {
  onPrivateShell();
  privatePush.preparePrivateRegistration.mockResolvedValueOnce(
    prepared({ content: 'generic', encryptionPublicKey: undefined, encryptionKeyId: undefined }),
  );
  await saveEnrollment(registration, context);
  expect(postedBodies()[0].private).toEqual({
    version: 1,
    content: 'generic',
    generation: 'G'.repeat(22),
  });
  expect(privatePush.commitPrivateRegistration).toHaveBeenCalledOnce();
});

it('cancels the prepared enrollment when the server refuses it', async () => {
  onPrivateShell();
  postReplies = [{ status: 400, body: { detail: 'invalid private enrollment' } }];
  await expect(saveEnrollment(registration, context)).rejects.toThrow('invalid private enrollment');
  expect(privatePush.cancelPrivateRegistration).toHaveBeenCalledWith({
    binding,
    generation: 'G'.repeat(22),
  });
  expect(privatePush.commitPrivateRegistration).not.toHaveBeenCalled();
  expect(connection.recordPushRegistration).not.toHaveBeenCalled();
});

it('restarts once from the sender key when the server says it changed', async () => {
  onPrivateShell();
  const rotated = { version: 1, key_id: 'T'.repeat(43), public_key: 'Q'.repeat(43) };
  senderKeys = [senderKey, rotated];
  postReplies = [{ status: 409, body: { detail: 'sender key changed' } }];
  privatePush.preparePrivateRegistration
    .mockResolvedValueOnce(prepared({ generation: 'G'.repeat(22) }))
    .mockResolvedValueOnce(prepared({ generation: 'H'.repeat(22) }));
  await saveEnrollment(registration, context);
  expect(privatePush.preparePrivateRegistration).toHaveBeenCalledTimes(2);
  expect(privatePush.preparePrivateRegistration).toHaveBeenLastCalledWith({
    binding,
    senderKey: rotated.public_key,
    senderKeyId: rotated.key_id,
  });
  expect(privatePush.cancelPrivateRegistration).toHaveBeenCalledOnce();
  expect(privatePush.cancelPrivateRegistration).toHaveBeenCalledWith({
    binding,
    generation: 'G'.repeat(22),
  });
  expect(postedBodies()[1].private.sender_key_id).toBe(rotated.key_id);
  expect(privatePush.commitPrivateRegistration).toHaveBeenCalledWith({
    binding,
    generation: 'H'.repeat(22),
    deviceId: 41,
  });
});

it('gives up after the one retry', async () => {
  onPrivateShell();
  postReplies = [
    { status: 409, body: { detail: 'sender key changed' } },
    { status: 409, body: { detail: 'sender key changed' } },
  ];
  await expect(saveEnrollment(registration, context)).rejects.toThrow('sender key changed');
  expect(posts()).toHaveLength(2);
  expect(privatePush.preparePrivateRegistration).toHaveBeenCalledTimes(2);
  expect(privatePush.cancelPrivateRegistration).toHaveBeenCalledTimes(2);
  expect(privatePush.commitPrivateRegistration).not.toHaveBeenCalled();
});

it('does not retry any other conflict', async () => {
  onPrivateShell();
  postReplies = [{ status: 409, body: { detail: 'private push not offered' } }];
  await expect(saveEnrollment(registration, context)).rejects.toThrow('private push not offered');
  expect(posts()).toHaveLength(1);
  expect(privatePush.cancelPrivateRegistration).toHaveBeenCalledOnce();
});

it('keeps the device registered without its key when the native commit fails', async () => {
  onPrivateShell();
  privatePush.commitPrivateRegistration.mockRejectedValueOnce(
    Object.assign(new Error('CONNECTION_CHANGED'), { code: 'CONNECTION_CHANGED' }),
  );
  await expect(saveEnrollment(registration, context)).rejects.toThrow('CONNECTION_CHANGED');
  expect(privatePush.cancelPrivateRegistration).toHaveBeenCalledWith({
    binding,
    generation: 'G'.repeat(22),
  });
  // The upsert reuses the row id, so a DELETE would also remove the working registration.
  expect(urls().some((u) => u.startsWith('DELETE'))).toBe(false);
  const bodies = postedBodies() as Record<string, unknown>[];
  expect(bodies).toHaveLength(2);
  expect(bodies[1]).not.toHaveProperty('private');
  expect(bodies[1]).toMatchObject({ connection_binding: binding });
  expect(connection.recordPushRegistration).toHaveBeenCalledWith({
    registrationId: 41,
    connectionBinding: binding,
    generation: 3,
  });
});

it('cancels without posting when the app returns an encrypted enrollment with no key', async () => {
  onPrivateShell();
  privatePush.preparePrivateRegistration.mockResolvedValueOnce(
    prepared({ encryptionPublicKey: undefined }),
  );
  await expect(saveEnrollment(registration, context)).rejects.toThrow();
  expect(posts()).toHaveLength(0);
  expect(privatePush.cancelPrivateRegistration).toHaveBeenCalledOnce();
});

it('keeps the plain path on a 0.14.0 shell even when the server offers private push', async () => {
  onPrivateShell('0.14.0');
  await saveEnrollment(registration, context);
  expect(urls()).toEqual(['POST /istota/api/push/devices']);
  expect(postedBodies()).toEqual([legacyBody('0.14.0')]);
  expect(privatePush.preparePrivateRegistration).not.toHaveBeenCalled();
});

it.each([
  ['delivery is unavailable', { push: { delivery: 'unavailable', private: { versions: [1] } } }],
  [
    'the server delivers through a relay',
    {
      push: {
        delivery: 'unavailable',
        private: { versions: [1] },
        relay: { versions: [1], url: 'https://relay.example.com/istota' },
      },
    },
  ],
  ['no private key is advertised', { push: { delivery: 'direct' } }],
  ['only another version is offered', { push: { delivery: 'direct', private: { versions: [2] } } }],
])('keeps the plain path when %s', async (_label, body) => {
  onPrivateShell();
  discovery = { status: 200, body };
  await saveEnrollment(registration, context);
  expect(postedBodies()).toEqual([legacyBody('0.15.0')]);
  expect(privatePush.preparePrivateRegistration).not.toHaveBeenCalled();
  expect(urls()).not.toContain('GET /istota/api/push/sender-key');
});

it('keeps the plain path when discovery cannot be read', async () => {
  onPrivateShell();
  discovery = { status: 503, body: { error: 'mobile_configuration_unavailable' } };
  await saveEnrollment(registration, context);
  expect(postedBodies()).toEqual([legacyBody('0.15.0')]);
  expect(privatePush.preparePrivateRegistration).not.toHaveBeenCalled();
});

it('refreshes a stale row through the private path on a 0.15.0 shell', async () => {
  onPrivateShell();
  devices = [device({ app_version: '0.14.0', token_tail: token.slice(-6) })];
  expect(await refreshStaleEnrollment()).toBe(true);
  expect(postedBodies()[0].private.content).toBe('encrypted');
  expect(privatePush.commitPrivateRegistration).toHaveBeenCalledOnce();
});
