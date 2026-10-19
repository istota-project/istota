import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import Page from './+page.svelte';
import { clearNotices, notices } from '$lib/stores/notices';

vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

function entryRow(over: Record<string, unknown> = {}) {
  return {
    uid: 'w1',
    index: 1,
    etag: 'etag-w1',
    date: '2026-09-14',
    client: 'acme',
    client_name: 'Acme Corp',
    service: 'dev',
    service_name: 'Development',
    service_type: 'hours',
    qty: 3,
    amount: null,
    discount: 0,
    description: 'API work',
    entity: '',
    separate: false,
    invoice: '',
    paid_date: null,
    computed_amount: 450,
    editable: true,
    deletable: true,
    warnings: [],
    ...over,
  };
}

const UNBILLED = entryRow();
const BILLED = entryRow({
  uid: 'w2',
  index: 2,
  etag: 'etag-w2',
  date: '2026-08-01',
  description: 'Design pass',
  invoice: 'INV-000010',
  editable: false,
  deletable: false,
});
const ON_VOIDED = entryRow({
  uid: 'w3',
  index: 3,
  etag: 'etag-w3',
  date: '2026-07-01',
  description: 'Billed then voided',
  deletable: false,
});

type Handler = (body: any) => Response | Promise<Response>;
let posts: { url: string; body: any }[];
let handlers: Record<string, Handler>;
let deletes: string[];
let deleteResponse: () => Response;
let listed: ReturnType<typeof entryRow>[];

beforeEach(() => {
  posts = [];
  handlers = {};
  deletes = [];
  deleteResponse = () => Response.json({ status: 'ok' });
  listed = [UNBILLED, BILLED];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace('/istota/api/money', '');
      if (init?.method === 'DELETE') {
        deletes.push(path);
        return deleteResponse();
      }
      if (init?.method === 'POST') {
        const body = init.body ? JSON.parse(String(init.body)) : null;
        posts.push({ url: path, body });
        const handler = handlers[path];
        if (!handler) throw new Error(`Unexpected POST: ${path}`);
        return handler(body);
      }
      if (path.startsWith('/work?'))
        return Response.json({
          status: 'ok',
          entries: listed,
          totals: { uninvoiced_count: 1, uninvoiced_amount: 450, invoiced_count: 1, paid_count: 0 },
        });
      if (path === '/clients') return Response.json({ status: 'ok', clients: [] });
      if (path === '/business-settings')
        return Response.json({ status: 'ok', entities: [], services: [], defaults: null });
      if (path.startsWith('/invoice-details'))
        return Response.json({ status: 'ok', invoice_number: 'INV-000010', items: [] });
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
});

afterEach(() => {
  cleanup();
  clearNotices();
  vi.unstubAllGlobals();
});

async function choose(rowIndex: number, label: string) {
  const triggers = await screen.findAllByLabelText('Actions');
  await fireEvent.keyDown(triggers[rowIndex], { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: label }));
}

function generated(dryRun: boolean) {
  return Response.json({
    status: 'ok',
    dry_run: dryRun,
    invoice_count: 1,
    total: 450,
    invoices: [
      {
        invoice_number: 'INV-000012',
        client_key: 'acme',
        client: 'Acme Corp',
        group: 'Development',
        items: 1,
        total: 450,
        terms: '30',
        entry_uids: ['w1'],
        entry_descriptions: ['API work'],
      },
    ],
    notifications: 'none',
  });
}

describe('Generate invoice', () => {
  it('previews the one entry with its etag, then issues it', async () => {
    handlers['/invoices/generate'] = (body) => generated(body.dry_run);
    render(Page);
    await choose(0, 'Generate invoice');
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText('INV-000012')).toBeTruthy();
    expect(posts[0].body).toEqual({
      entry_uids: ['w1'],
      etags: { w1: 'etag-w1' },
      dry_run: true,
    });

    await fireEvent.click(within(dialog).getByRole('button', { name: 'Issue 1 invoice' }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].body).toEqual({
      entry_uids: ['w1'],
      etags: { w1: 'etag-w1' },
      dry_run: false,
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('explains a stale entry rather than issuing', async () => {
    handlers['/invoices/generate'] = () =>
      Response.json(
        { status: 'error', error: 'work entries changed since they were read', stale: ['w1'] },
        { status: 409 },
      );
    render(Page);
    await choose(0, 'Generate invoice');
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/changed since/);
    expect(within(dialog).getByRole('button', { name: /^Issue/ })).toBeDisabled();
    expect(posts).toHaveLength(1);
  });
});

describe('an invoiced entry', () => {
  it('offers to void its invoice, through the same dialog as the invoices page', async () => {
    handlers['/invoices/INV-000010/void'] = () =>
      Response.json({ status: 'ok', invoice: {}, entries_released: 1 });
    render(Page);
    await choose(1, 'Void INV-000010…');
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Void INV-000010')).toBeTruthy();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Void invoice' }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({
      url: '/invoices/INV-000010/void',
      body: { reason: '', replaced_by: [], force: false },
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});

describe('deleting an entry', () => {
  it('does not offer Delete for an entry a voided invoice lists', async () => {
    listed = [UNBILLED, ON_VOIDED];
    render(Page);
    const triggers = await screen.findAllByLabelText('Actions');
    await fireEvent.keyDown(triggers[1], { key: 'Enter' });
    const item = await screen.findByRole('menuitem', { name: /listed on a voided invoice/ });
    expect(item).toHaveAttribute('aria-disabled', 'true');
    expect(screen.queryByRole('menuitem', { name: 'Delete' })).toBeNull();
  });

  it('says why a voided invoice blocks it, not that the entry changed', async () => {
    deleteResponse = () =>
      Response.json(
        {
          status: 'error',
          error: 'entry is listed on a voided invoice',
          code: 'on_voided_invoice',
        },
        { status: 409 },
      );
    render(Page);
    await choose(0, 'Delete');
    const confirm = await screen.findByRole('dialog');
    await fireEvent.click(within(confirm).getByRole('button', { name: /Delete/ }));
    await waitFor(() => expect(deletes).toHaveLength(1));
    await waitFor(() =>
      expect(get(notices).map((n) => n.message)).toEqual([
        expect.stringMatching(/listed on a voided invoice/),
      ]),
    );
    expect(screen.queryByText(/changed elsewhere/)).toBeNull();
  });

  it('still reports a stale etag as changed elsewhere', async () => {
    deleteResponse = () =>
      Response.json(
        { status: 'error', error: 'entry changed', code: 'conflict', entry: null },
        { status: 409 },
      );
    render(Page);
    await choose(0, 'Delete');
    const confirm = await screen.findByRole('dialog');
    await fireEvent.click(within(confirm).getByRole('button', { name: /Delete/ }));
    expect(await screen.findByText(/changed elsewhere/)).toBeTruthy();
  });
});

describe('sorting and download', () => {
  const LATER_BILL = entryRow({
    uid: 'w4',
    index: 4,
    etag: 'etag-w4',
    date: '2026-09-20',
    description: 'Hotfix',
    invoice: 'INV-000009',
    editable: false,
    deletable: false,
  });

  beforeEach(() => {
    localStorage.clear();
    listed = [UNBILLED, BILLED, LATER_BILL];
  });

  function order(): string[] {
    return [...document.querySelectorAll('.record-row .work-desc')].map(
      (el) => el.textContent?.trim() ?? '',
    );
  }

  it('sorts by date, newest first, until told otherwise', async () => {
    render(Page);
    await screen.findByText('Hotfix');
    expect(order()).toEqual(['Hotfix', 'API work', 'Design pass']);
  });

  it('sorts by invoice number, keeping uninvoiced entries last both ways', async () => {
    render(Page);
    await screen.findByText('Hotfix');
    await fireEvent.click(screen.getByRole('button', { name: 'Sort by invoice number' }));
    expect(order()).toEqual(['Design pass', 'Hotfix', 'API work']);
    await fireEvent.click(screen.getByRole('button', { name: /Sorted by invoice number/ }));
    expect(order()).toEqual(['Hotfix', 'Design pass', 'API work']);
  });

  it('remembers the sort across visits', async () => {
    render(Page);
    await screen.findByText('Hotfix');
    await fireEvent.click(screen.getByRole('button', { name: 'Sort by invoice number' }));
    cleanup();
    render(Page);
    await screen.findByText('Hotfix');
    expect(order()).toEqual(['Design pass', 'Hotfix', 'API work']);
  });

  it('offers CSV and JSON downloads of the work log', async () => {
    render(Page);
    await screen.findByText('Hotfix');
    await fireEvent.keyDown(screen.getByLabelText('Download work log'), { key: 'Enter' });
    const csv = await screen.findByRole('menuitem', { name: 'Download as CSV' });
    expect(csv).toHaveAttribute('href', '/istota/api/money/work/export?format=csv');
    expect(csv).toHaveAttribute('download', '');
    expect(
      screen.getByRole('menuitem', { name: 'Download as JSON (full record)' }),
    ).toHaveAttribute('href', '/istota/api/money/work/export?format=json');
  });
});
