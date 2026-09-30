import { expect, it } from 'vitest';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { ViteDevServer } from 'vite';
import { mockApi } from '../../../vite-mock-api';

it('serves users and persists attachment and access actions through the mock middleware', async () => {
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
  function request(method: string, path = '', body?: unknown) {
    return new Promise<{ status: number; body: any }>((resolve) => {
      const req = Readable.from(body ? [Buffer.from(JSON.stringify(body))] : []) as IncomingMessage;
      req.method = method;
      req.url = `/istota/api/admin/users${path}`;
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
  const initial = await request('GET');
  expect(initial.status).toBe(200);
  const legacy = initial.body.users.find((user: any) => user.state === 'nextcloud_only');
  expect(legacy).toBeTruthy();
  expect(
    (await request('POST', '', { user_id: legacy.user_id, email: 'legacy@example.com' })).body,
  ).toEqual({ sent: true });
  const attached = (await request('GET')).body.users.find(
    (user: any) => user.user_id === legacy.user_id,
  );
  expect(attached.identity.email).toBe('legacy@example.com');
  expect(attached.display_name).toBe(legacy.display_name);
  expect((await request('POST', `/${legacy.user_id}/disable`, { disabled: true })).status).toBe(
    200,
  );
  expect(
    (await request('GET')).body.users.find((user: any) => user.user_id === legacy.user_id).identity
      .disabled,
  ).toBe(true);
  expect((await request('DELETE', `/${legacy.user_id}`)).status).toBe(200);
  expect(
    (await request('GET')).body.users.find((user: any) => user.user_id === legacy.user_id).identity,
  ).toBeNull();
});
