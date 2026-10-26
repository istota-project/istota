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
  return function request(method: string, path: string, json?: unknown) {
    return new Promise<{ status: number; body: any; headers: Record<string, string> }>(
      (resolve) => {
        const chunks = json === undefined ? [] : [Buffer.from(JSON.stringify(json))];
        const req = Readable.from(chunks) as IncomingMessage;
        req.method = method;
        req.url = `/istota/api${path}`;
        const headers: Record<string, string> = {};
        const res = {
          statusCode: 200,
          setHeader(name: string, value: string) {
            headers[name] = value;
          },
          end(raw: string) {
            const text = String(raw ?? '');
            resolve({
              status: res.statusCode,
              body: text.startsWith('{') ? JSON.parse(text) : text,
              headers,
            });
          },
        } as unknown as ServerResponse;
        middleware(req, res, () => resolve({ status: 404, body: null, headers }));
      },
    );
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

it('serves an app view: detail, a sandboxed frame, kv:own calls and uninstall', async () => {
  const request = mockServer();
  const detail = await request('GET', '/apps/board');
  expect(detail.status).toBe(200);
  expect(detail.body.name).toBe('Board');
  const version = detail.body.current_version;

  const frame = await request('GET', `/apps/board/v/${version}/frame`);
  expect(frame.status).toBe(200);
  expect(frame.headers['Content-Type']).toMatch(/^text\/html/);
  expect(frame.headers['Content-Security-Policy']).toMatch(/sandbox allow-scripts$/);
  expect(frame.body).toContain('istota.kv.get');
  expect((await request('GET', '/apps/board/v/99/frame')).status).toBe(409);

  const call = `/apps/board/v/${version}/call`;
  await request('POST', call, { capability: 'kv:own', op: 'set', args: { key: 'k', value: 3 } });
  const got = await request('POST', call, { capability: 'kv:own', op: 'get', args: { key: 'k' } });
  expect(got.body).toEqual({ result: 3 });
  const refused = await request('POST', call, { capability: 'calendar:read', op: 'list' });
  expect(refused.status).toBe(403);
  expect(refused.body.error.code).toBe('not_granted');

  expect((await request('DELETE', '/apps/board')).status).toBe(204);
  expect((await request('GET', '/apps/board')).status).toBe(404);
});

it('serves the approval screen, approves against the shown list, and rolls back', async () => {
  const request = mockServer();
  const shown = await request('GET', '/apps/tides/pending/mock-pending-tides');
  expect(shown.status).toBe(200);
  expect(shown.body.one_button).toBe(false);
  expect(shown.body.new_capabilities).toEqual(['calendar:read', 'skill:health.trend']);
  expect(shown.body.capabilities.some((c: { sensitive: boolean }) => c.sensitive)).toBe(true);

  const stale = await request('POST', '/apps/tides/pending/mock-pending-tides/approve', {
    new_capabilities: ['calendar:read'],
  });
  expect(stale.status).toBe(409);
  expect(stale.body.error).toBe('stale');

  const approved = await request('POST', '/apps/tides/pending/mock-pending-tides/approve', {
    new_capabilities: shown.body.new_capabilities,
  });
  expect(approved.body).toEqual({ app_id: 'tides', version: 3 });
  expect((await request('GET', '/apps/tides/pending/mock-pending-tides')).status).toBe(404);

  const versions = await request('GET', '/apps/tides/versions');
  expect(versions.body.versions.map((v: { version: number }) => v.version)).toEqual([1, 2, 3]);
  expect((await request('DELETE', '/apps/tides/versions/3')).status).toBe(409);
  await request('POST', '/apps/tides/versions/2/current');
  expect((await request('GET', '/apps/tides')).body.current_version).toBe(2);
  expect((await request('DELETE', '/apps/tides/versions/3')).status).toBe(204);
});

it('grants a starter app its folder on first open, and reports storage', async () => {
  const request = mockServer();
  const before = await request('GET', '/apps/recall');
  expect(before.body.ungranted.map((c: { capability: string }) => c.capability)).toEqual([
    'files:read:notes',
  ]);
  const refused = await request('POST', '/apps/recall/grants', {
    capability: 'files:read:notes',
    scope: 'memories',
  });
  expect(refused.status).toBe(422);
  const granted = await request('POST', '/apps/recall/grants', {
    capability: 'files:read:notes',
    scope: 'notes/aviation',
  });
  expect(granted.body).toEqual({ capability: 'files:read:notes', scope: 'notes/aviation' });
  const after = await request('GET', '/apps/recall');
  expect(after.body.ungranted).toEqual([]);
  expect(after.body.grants).toContainEqual(
    expect.objectContaining({ capability: 'files:read:notes', scope: 'notes/aviation' }),
  );

  const storage = await request('GET', '/apps/storage');
  expect(storage.body.apps.length).toBeGreaterThan(0);
  expect(storage.body.apps[0]).toHaveProperty('kv_bytes');
});
