import { expect, it } from 'vitest';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { ViteDevServer } from 'vite';
import { mockApi } from '../../../../vite-mock-api';

it('serves the sharing card and the shared folder place on the mock dev server', async () => {
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

  const me = await request('GET', '/me');
  expect(me.body.features).toMatchObject({ sharing: true, sharing_ocm: true });

  const sharing = await request('GET', '/settings/sharing');
  expect(sharing.status).toBe(200);
  expect(sharing.body.ocm.federated_id).toMatch(/@/);
  expect(sharing.body.ocm.pending).toHaveLength(1);
  expect(JSON.stringify(sharing.body)).not.toMatch(/\.ics|secret/i);

  const offer = sharing.body.ocm.pending[0].id;
  const accepted = await request('POST', `/settings/sharing/folders/${offer}/accept`, {
    allow_scheduled: false,
  });
  expect(accepted.body.share.id).toBe(offer);
  const again = await request('POST', `/settings/sharing/folders/${offer}/accept`, {
    allow_scheduled: false,
  });
  expect(again.status).toBe(409);

  const places = await request('GET', '/workspace/places');
  expect(places.body.share_index).toBe(true);
  const share = places.body.places.find((place: any) => place.kind === 'share');
  expect(share).toMatchObject({ browsable: true, remote: { host: 'cloud.example.org' } });
  // #849: a share is browsed like a folder, its entries carrying their share path.
  const listing = await request('GET', `/workspace/list?place=${encodeURIComponent(share.id)}`);
  expect(listing.status).toBe(200);
  expect(listing.body.entries[0].share_path).toBe('note.md');
});
