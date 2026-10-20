import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { join } from 'node:path';
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

// The mock keeps its own copy of BLOCK_TEMPLATES; this holds its ids, in order, to the backend's.
function backendTemplateIds(): string[] {
  const source = readFileSync(
    join(process.cwd(), '..', 'src', 'istota', 'briefings', 'models.py'),
    'utf8',
  );
  const start = source.indexOf('BLOCK_TEMPLATES = [');
  const end = source.indexOf('\n]\n', start);
  if (start < 0 || end < 0) throw new Error('BLOCK_TEMPLATES not found in models.py');
  const ids = [...source.slice(start, end).matchAll(/^\s+\("(\w+)", "/gm)].map((m) => m[1]);
  if (ids.length === 0) throw new Error('No template ids parsed from BLOCK_TEMPLATES');
  return ids;
}

async function api(path: string, method = 'GET', body?: unknown) {
  const response = await fetch(origin + path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, data: await response.json() };
}

describe('briefing development data', () => {
  it('has structured and legacy editions across named briefings and mixed widths', async () => {
    const { data } = await api('/briefings/archive?limit=100');
    expect(data.total).toBeGreaterThanOrEqual(12);
    expect(data.briefing_names).toEqual(expect.arrayContaining(['morning', 'evening', 'weekly']));
    const details = await Promise.all(
      data.items.map((row: { id: number }) => api(`/briefings/archive/${row.id}`)),
    );
    const structured = details.map((r) => r.data).filter((row) => row.document);
    expect(structured[0].document.blocks.length).toBeGreaterThanOrEqual(6);
    const cards = structured.flatMap((row) => row.document.blocks);
    expect(new Set(cards.map((card: any) => card.presentation.width))).toEqual(
      new Set(['auto', 'wide', 'full']),
    );
    expect(cards.some((card: any) => card.source_status === 'partial')).toBe(true);
    expect(details.some((r) => r.data.document === null)).toBe(true);
    expect(
      data.items.every((row: any) => !('document' in row) && typeof row.body_md === 'string'),
    ).toBe(true);
  });

  it('provides presets, disabled schedules and atomic saved sources', async () => {
    const { data: config } = await api('/briefings/config');
    expect(config.block_templates.map((row: { id: string }) => row.id)).toEqual(
      backendTemplateIds(),
    );
    const schedules = await api('/settings/briefings');
    expect(schedules.data.briefings.some((row: any) => !row.enabled)).toBe(true);
    const block = config.briefings.find((row: any) => row.name === 'morning').blocks[0];
    const { status, data } = await api('/briefings/blocks', 'PUT', {
      id: block.id,
      title: block.title,
      options: { ...block.options, presentation: { width: 'full' } },
      sources: block.sources,
    });
    expect(status).toBe(200);
    expect(data.block.options.presentation.width).toBe('full');
    expect(data.block.sources).toEqual(block.sources);
    const copy = await api('/briefings/blocks', 'PUT', {
      briefing_name: 'morning',
      title: 'Copied headlines',
      options: data.block.options,
      sources: data.block.sources.map(({ id: _id, ...source }: any) => source),
    });
    expect(copy.data.block.sources.length).toBe(block.sources.length);
    expect(copy.data.block.sources[0].id).not.toBe(block.sources[0].id);
  });

  it('renders unsaved preview content with production email semantics', async () => {
    const document = {
      version: 1,
      title: 'Draft preview',
      blocks: [
        {
          block_id: 901,
          title: 'Draft <section>',
          kind: 'markdown',
          body_md: 'A [source](https://example.com/a_b?x=one_two).',
          presentation: { width: 'wide' },
          source_status: 'partial',
        },
      ],
    };
    const { status, data } = await api('/briefings/preview', 'POST', { document });
    expect(status).toBe(200);
    expect(data.html).toContain('<h2>Draft &lt;section&gt;</h2>');
    expect(data.plain).toContain('source (https://example.com/a_b?x=one_two)');
    expect(data.body_md).toContain(document.blocks[0].body_md);
  });
});

it('filters previews by briefing_name and accepts the builder reorder payload', async () => {
  const filtered = await api('/briefings/archive?briefing_name=evening&limit=1');
  expect(filtered.data.total).toBe(4);
  expect(filtered.data.items).toHaveLength(1);
  expect(filtered.data.items[0].briefing_name).toBe('evening');
  const { data: config } = await api('/briefings/config');
  const current = config.briefings.find((row: any) => row.name === 'morning').blocks;
  const ids = current.map((row: any) => row.id).reverse();
  const result = await api('/briefings/blocks', 'PUT', {
    reorder: { briefing_name: 'morning', ordered_ids: ids },
  });
  expect(result.status).toBe(200);
  const refreshed = await api('/briefings/config');
  expect(
    refreshed.data.briefings
      .find((row: any) => row.name === 'morning')
      .blocks.map((row: any) => row.id),
  ).toEqual(ids);
});
