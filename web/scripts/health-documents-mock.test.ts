import { afterAll, beforeAll, expect, it } from 'vitest';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { mockApi } from '../vite-mock-api';

type Middleware = (req: IncomingMessage, res: ServerResponse, next: () => void) => void;
let server: ReturnType<typeof createServer>;
let origin = '';

beforeAll(async () => {
  let middleware: Middleware | undefined;
  const configure = mockApi().configureServer;
  if (typeof configure !== 'function') throw new Error('Missing mock middleware');
  (configure as (server: unknown) => void)({
    middlewares: {
      use(handler: Middleware) {
        middleware = handler;
      },
    },
  });
  server = createServer((req, res) =>
    middleware!(req, res, () => {
      res.statusCode = 404;
      res.end();
    }),
  );
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Missing server address');
  origin = `http://127.0.0.1:${address.port}/istota/api`;
});

afterAll(async () => {
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

async function api(path: string, method = 'GET', body?: unknown) {
  const response = await fetch(origin + path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, data: await response.json() };
}

it('lists resolved attachments and preserves them in document details', async () => {
  const { data } = await api('/health/documents');
  const attached = data.documents.find((doc: { id: number }) => doc.id === 1);
  expect(attached.links).toHaveLength(2);
  expect(attached.links[0]).toEqual(
    expect.objectContaining({ entity_type: 'encounter', entity_id: 1, label: expect.any(String) }),
  );
  const detail = await api('/health/documents/1');
  expect(detail.data.links).toEqual(attached.links);
  expect(data.documents.find((doc: { id: number }) => doc.id === 3).links).toEqual([]);
});
