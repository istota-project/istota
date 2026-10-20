import { expect, it } from 'vitest';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { ViteDevServer } from 'vite';
import { mockApi } from '../../vite-mock-api';

it('mints a device and writes quick notes on the mock, refusing both with no folder', async () => {
  let middleware: (req: IncomingMessage, res: ServerResponse, next: () => void) => void;
  const plugin = mockApi();
  const configure = plugin.configureServer as (server: ViteDevServer) => void;
  configure({
    middlewares: {
      use: (handler: typeof middleware) => {
        middleware = handler;
      },
    },
  } as ViteDevServer);
  function request(method: string, path: string, body?: unknown) {
    return new Promise<{ status: number; body: any }>((resolve) => {
      const req = Readable.from(body ? [Buffer.from(JSON.stringify(body))] : []) as IncomingMessage;
      req.method = method;
      req.url = `/istota/api${path}`;
      const res = {
        statusCode: 200,
        setHeader() {},
        end(raw: string) {
          resolve({ status: res.statusCode, body: raw.startsWith('{') ? JSON.parse(raw) : raw });
        },
      } as unknown as ServerResponse;
      middleware(req, res, () => resolve({ status: 404, body: null }));
    });
  }
  const device = {
    device_id: '0f8fad5b-d9cb-469f-a165-70867728950e',
    label: 'iPhone',
    scopes: ['notes'],
  };

  const unset = await request('POST', '/workspace/notes', { content: 'Buy milk' });
  expect(unset.status).toBe(409);
  expect(unset.body.code).toBe('notes_folder_unset');
  expect((await request('POST', '/devices', device)).body.code).toBe('notes_folder_unset');

  await request('PUT', '/settings/profile', { quick_notes_folder: 'Notes/Inbox' });
  const minted = await request('POST', '/devices', device);
  expect(minted.status).toBe(201);
  expect(minted.body.token).toHaveLength(43);
  const listed = (await request('GET', '/devices')).body.devices;
  expect(listed.filter((d: any) => d.device_id === device.device_id.toUpperCase())).toHaveLength(1);
  expect((await request('DELETE', '/devices/self')).status).toBe(200);

  const body = { content: '# Buy milk\neggs', client_note_id: 'mock-key-0001' };
  const created = await request('POST', '/workspace/notes', body);
  expect(created.status).toBe(201);
  expect(created.body).toMatchObject({ path: 'Notes/Inbox/Buy milk.md', created: true });
  const replay = await request('POST', '/workspace/notes', body);
  expect(replay.status).toBe(200);
  expect(replay.body).toMatchObject({ path: created.body.path, created: false });

  const saved = await request('PUT', '/workspace/notes', {
    path: created.body.path,
    content: '# Buy milk\neggs\nbread',
    revision: created.body.revision,
  });
  expect(saved.status).toBe(200);
  const stale = await request('PUT', '/workspace/notes', {
    path: created.body.path,
    content: 'x',
    revision: created.body.revision,
  });
  expect(stale.status).toBe(409);
  expect(stale.body.code).toBe('conflict');
});
