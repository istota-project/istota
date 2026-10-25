import { expect, it } from 'vitest';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { ViteDevServer } from 'vite';
import { mockApi } from '../../../vite-mock-api';

function mockServer() {
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
  return function request(method: string, path: string) {
    return new Promise<{ status: number; body: any }>((resolve) => {
      const req = Readable.from([]) as IncomingMessage;
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
  };
}

it('serves the apps list, builds a pending install and refuses an invalid draft', async () => {
  const request = mockServer();
  const listed = await request('GET', '/apps/');
  expect(listed.status).toBe(200);
  expect(listed.body.kit_built).toBe(true);
  expect(listed.body.drafts.map((d: { app_id: string }) => d.app_id)).toContain('packing');

  const built = await request('POST', '/apps/drafts/packing/pending');
  expect(built.status).toBe(201);
  expect(built.body).toEqual({ pending_id: 'mock-pending-packing', app_id: 'packing' });

  const refused = await request('POST', '/apps/drafts/broken/pending');
  expect(refused.status).toBe(422);
  expect(refused.body.errors.length).toBeGreaterThan(0);

  const reinstalled = await request('POST', '/apps/starter/ledger/reinstall');
  expect(reinstalled.body).toEqual({ app_id: 'ledger', status: 'installed' });
  const after = await request('GET', '/apps/');
  expect(after.body.apps.map((a: { app_id: string }) => a.app_id)).toContain('ledger');
});
