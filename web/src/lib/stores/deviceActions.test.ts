import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { resetDeviceActions, syncDeviceActions } from './deviceActions';
vi.mock('$app/paths', () => ({ base: '/istota' }));

const PHONE = '6f9619ff-8b86-d011-b42d-00c04fc964ff';
const status = {
  deviceId: PHONE,
  scopes: [] as string[],
  tokenRejected: false,
  pendingNotes: 0,
  failedNotes: 0,
};
const plugin = {
  status: vi.fn(async () => ({ ...status })),
  provision: vi.fn(async () => ({})),
  forget: vi.fn(async () => ({})),
  consumePendingOpen: vi.fn(async () => ({ open: null })),
};
let folder: string | null = 'Notes/Inbox';
let devices: Record<string, unknown>[] = [];
const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  const method = init?.method ?? 'GET';
  if (url === '/istota/api/settings/profile') {
    return new Response(JSON.stringify({ profile: { quick_notes_folder: folder } }));
  }
  if (url === '/istota/api/devices' && method === 'POST') {
    const body = JSON.parse(init!.body as string);
    devices = [{ id: 7, device_id: body.device_id.toUpperCase(), scopes: body.scopes }];
    return new Response(JSON.stringify({ id: 7, token: 'minted-token', scopes: body.scopes }), {
      status: 201,
    });
  }
  if (url === '/istota/api/devices') return new Response(JSON.stringify({ devices }));
  if (url.startsWith('/istota/api/devices/') && method === 'DELETE') {
    return new Response(JSON.stringify({ ok: true }));
  }
  return new Response('{}', { status: 404 });
});
function calls(method: string, url: string) {
  return fetchMock.mock.calls.filter(
    ([u, init]) => u === url && (init?.method ?? 'GET') === method,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  resetDeviceActions();
  status.scopes = [];
  status.tokenRejected = false;
  folder = 'Notes/Inbox';
  devices = [];
  Object.defineProperty(navigator, 'userAgent', {
    value: 'Mozilla/5.0 (iPhone) IstotaApp/0.13.0',
    configurable: true,
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('Capacitor', { Plugins: { IstotaActions: plugin } });
});
afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, 'userAgent', { value: 'Mozilla/5.0', configurable: true });
});

it('mints a notes token and provisions it when the folder is set and the shell holds none', async () => {
  await syncDeviceActions();
  const posts = calls('POST', '/istota/api/devices');
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0][1]!.body as string)).toEqual({
    device_id: PHONE,
    label: 'iPhone',
    scopes: ['notes'],
  });
  expect(plugin.provision).toHaveBeenCalledWith({
    token: 'minted-token',
    scopes: ['notes'],
    notesFolder: 'Notes/Inbox',
  });
});

it('does not mint when the shell already holds the wanted scopes', async () => {
  status.scopes = ['notes'];
  devices = [{ id: 3, device_id: PHONE.toUpperCase(), scopes: ['notes'] }];
  await syncDeviceActions();
  await syncDeviceActions();
  expect(calls('POST', '/istota/api/devices')).toHaveLength(0);
  // One label-only provision for the page, since the status carries no label.
  expect(plugin.provision).toHaveBeenCalledTimes(1);
  expect(plugin.provision).toHaveBeenCalledWith({
    token: null,
    scopes: ['notes'],
    notesFolder: 'Notes/Inbox',
  });
});

it("mints again when the shell's token is not this account's", async () => {
  // Another account signed in under this shell connection and minted the token it holds.
  status.scopes = ['notes'];
  devices = [{ id: 4, device_id: 'OTHER-PHONE', scopes: ['notes'] }];
  await syncDeviceActions();
  expect(calls('POST', '/istota/api/devices')).toHaveLength(1);
  expect(plugin.provision).toHaveBeenCalledTimes(1);
  expect(plugin.provision).toHaveBeenCalledWith({
    token: 'minted-token',
    scopes: ['notes'],
    notesFolder: 'Notes/Inbox',
  });
});

it('mints again when the shell says its token was rejected', async () => {
  status.scopes = ['notes'];
  status.tokenRejected = true;
  await syncDeviceActions();
  expect(calls('POST', '/istota/api/devices')).toHaveLength(1);
  expect(plugin.provision).toHaveBeenCalledWith(expect.objectContaining({ token: 'minted-token' }));
});

it('forgets the token and deletes this device row when the folder is cleared', async () => {
  status.scopes = ['notes'];
  folder = null;
  devices = [
    { id: 3, device_id: PHONE.toUpperCase(), scopes: ['notes'] },
    { id: 4, device_id: 'OTHER-PHONE', scopes: ['notes'] },
  ];
  await syncDeviceActions();
  expect(plugin.forget).toHaveBeenCalledTimes(1);
  expect(calls('DELETE', '/istota/api/devices/3')).toHaveLength(1);
  expect(calls('DELETE', '/istota/api/devices/4')).toHaveLength(0);
  expect(calls('POST', '/istota/api/devices')).toHaveLength(0);
  expect(plugin.provision).not.toHaveBeenCalled();
});

it('does nothing when no folder is set and the shell holds nothing', async () => {
  folder = null;
  await syncDeviceActions();
  expect(plugin.forget).not.toHaveBeenCalled();
  expect(plugin.provision).not.toHaveBeenCalled();
  expect(calls('GET', '/istota/api/devices')).toHaveLength(0);
});

it('changes only the label, keeping the token, when the folder changes', async () => {
  await syncDeviceActions();
  status.scopes = ['notes'];
  folder = 'Notes/Other';
  await syncDeviceActions();
  expect(calls('POST', '/istota/api/devices')).toHaveLength(1);
  expect(plugin.provision).toHaveBeenLastCalledWith({
    token: null,
    scopes: ['notes'],
    notesFolder: 'Notes/Other',
  });
});

it('runs two racing syncs one after the other, never interleaved', async () => {
  await Promise.all([syncDeviceActions(), syncDeviceActions()]);
  const order = fetchMock.mock.calls.map(([u, init]) => `${init?.method ?? 'GET'} ${u}`);
  expect(order).toEqual([
    'GET /istota/api/settings/profile',
    'POST /istota/api/devices',
    'GET /istota/api/settings/profile',
    'POST /istota/api/devices',
  ]);
});

it('is inert off-shell and on an older shell', async () => {
  for (const ua of ['Mozilla/5.0', 'IstotaApp/0.12.0']) {
    Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
    await syncDeviceActions();
  }
  expect(fetchMock).not.toHaveBeenCalled();
  expect(plugin.status).not.toHaveBeenCalled();
});

it('logs and swallows a failed mint', async () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  fetchMock.mockImplementationOnce(
    async () => new Response(JSON.stringify({ profile: { quick_notes_folder: 'Notes' } })),
  );
  fetchMock.mockImplementationOnce(
    async () =>
      new Response(JSON.stringify({ error: 'Too many devices', code: 'too_many_devices' }), {
        status: 409,
      }),
  );
  await expect(syncDeviceActions()).resolves.toBeUndefined();
  expect(plugin.provision).not.toHaveBeenCalled();
  expect(warn).toHaveBeenCalled();
  warn.mockRestore();
});
