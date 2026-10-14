import { expect, it } from 'vitest';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { ViteDevServer } from 'vite';
import { mockApi } from '../../../../vite-mock-api';

const CONTACTS_RO = 'https://www.googleapis.com/auth/contacts.readonly';
const OTHER_RO = 'https://www.googleapis.com/auth/contacts.other.readonly';

it('offers Contacts read-only and keeps the shared other-contacts scope read-only', async () => {
  let middleware: (req: IncomingMessage, res: ServerResponse, next: () => void) => void;
  const configure = mockApi().configureServer as (server: ViteDevServer) => void;
  configure({
    middlewares: {
      use: (handler: typeof middleware) => {
        middleware = handler;
      },
    },
  } as ViteDevServer);
  function request(method: string, path: string, body?: unknown) {
    return new Promise<any>((resolve) => {
      const req = Readable.from(body ? [Buffer.from(JSON.stringify(body))] : []) as IncomingMessage;
      req.method = method;
      req.url = `/istota/api/google/${path}`;
      const res = {
        statusCode: 200,
        setHeader() {},
        end(raw: string) {
          resolve(JSON.parse(raw));
        },
      } as unknown as ServerResponse;
      middleware(req, res, () => resolve(null));
    });
  }
  const status = await request('GET', 'status');
  expect(status.offered).toContainEqual({
    service: 'contacts',
    label: 'Contacts',
    max_level: 'readonly',
  });
  expect(status.granted.find((row: any) => row.service === 'contacts')).toMatchObject({
    level: 'readonly',
    complete: true,
    scopes: [CONTACTS_RO, OTHER_RO],
  });
  const saved = await request('PUT', 'scopes', { selection: { contacts: 'full' } });
  expect(saved.requested_scopes).toEqual([CONTACTS_RO, OTHER_RO]);
  expect(saved.reconnect_required).toBe(false);
});
