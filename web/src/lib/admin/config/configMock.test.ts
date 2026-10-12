import { expect, it, vi } from 'vitest';
import { Readable } from 'node:stream';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { ViteDevServer } from 'vite';
import { mockApi } from '../../../../vite-mock-api';

/** The dev server's config editor: preview, submit, and a status that walks
 *  the way the host applier moves a request, with a `started_at` that moves
 *  after it is applied. */
it('walks a change from preview to applied and moves started_at', async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2099-01-01T08:00:00Z'));
  let middleware: (req: IncomingMessage, res: ServerResponse, next: () => void) => void;
  const plugin = mockApi();
  (plugin.configureServer as (server: ViteDevServer) => void)({
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
          resolve({ status: res.statusCode, body: raw ? JSON.parse(raw) : raw });
        },
      } as unknown as ServerResponse;
      middleware(req, res, () => resolve({ status: 404, body: null }));
    });
  }

  try {
    const view = (await request('GET', '/admin/config')).body;
    expect(view.editable).toBe(true);
    const field = view.sections
      .flatMap((s: any) => s.fields)
      .find((f: any) => f.key === 'logging.level');
    expect(field).toMatchObject({ path: ['logging', 'level'], editor: 'choice', in_file: true });
    const startedAt = view.started_at;

    const changeSet = {
      base_digest: view.document_digest,
      ops: [{ op: 'set', path: ['logging', 'level'], value: 'DEBUG' }],
      raw: null,
      secrets: {},
    };
    const stale = await request('POST', '/admin/config/preview', {
      ...changeSet,
      base_digest: 'old',
    });
    expect(stale).toMatchObject({ status: 409, body: { error: 'config_changed' } });
    const preview = await request('POST', '/admin/config/preview', changeSet);
    expect(preview.body).toMatchObject({ changed: true, changed_sections: ['logging'] });

    const refused = await request('POST', '/admin/config/requests', changeSet);
    expect(refused).toMatchObject({ status: 403, body: { field: 'code' } });
    const submitted = await request('POST', '/admin/config/requests', {
      ...changeSet,
      step_up: { request_id: 'mock-step-up', code: '123456' },
    });
    expect(submitted.status).toBe(202);
    const id = submitted.body.id;

    const seen: string[] = [];
    for (let i = 0; i < 6; i += 1) {
      seen.push((await request('GET', `/admin/config/requests/${id}`)).body.status);
    }
    expect(seen).toEqual(['pending', 'taken', 'taken', 'applied', 'applied', 'applied']);

    expect((await request('GET', '/admin/config')).body.started_at).toBe(startedAt);
    vi.setSystemTime(new Date('2099-01-01T08:00:05Z'));
    const after = (await request('GET', '/admin/config')).body;
    expect(after.started_at > startedAt).toBe(true);
    expect(
      after.sections.flatMap((s: any) => s.fields).find((f: any) => f.key === 'logging.level')
        .value,
    ).toBe('DEBUG');

    // The history lists the change, and restoring the seeded one previews it as raw text.
    const history = (await request('GET', '/admin/config/requests?limit=50')).body.requests;
    expect(history.map((r: any) => [r.id, r.status])).toEqual([
      [id, 'applied'],
      [1, 'applied'],
    ]);
    const restored = (await request('POST', '/admin/config/requests/1/restore', {})).body;
    expect(restored.change_set).toMatchObject({ ops: [], raw: history[1].document_toml });
    expect(restored.preview.changed).toBe(true);
    const broken = await request('POST', '/admin/config/preview', {
      ...restored.change_set,
      raw: 'level = = 1\n',
    });
    expect(broken).toMatchObject({ status: 400, body: { error: 'invalid_change' } });
    expect(broken.body.errors[0]).toMatch(/does not parse.*line 1/);
  } finally {
    vi.useRealTimers();
  }
});
