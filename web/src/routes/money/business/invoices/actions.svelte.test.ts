import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import Page from './+page.svelte';
import FileViewerHost from '$lib/fileViewer/FileViewerHost.svelte';
import { viewer } from '$lib/fileViewer/store.svelte';

vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));
vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: {},
  getDocument: vi.fn(() => ({ promise: new Promise(() => {}), destroy: async () => {} })),
}));

function invoiceRow(over: Record<string, unknown> = {}) {
  return {
    invoice_number: 'INV-000010',
    client: 'Acme Corp',
    client_key: 'acme',
    date: '2026-09-30',
    total: 1500,
    status: 'outstanding',
    voided_at: null,
    void_reason: '',
    replaced_by: [],
    has_pdf: true,
    ...over,
  };
}

const OPEN = invoiceRow();
const PAID = invoiceRow({
  invoice_number: 'INV-000011',
  status: 'paid',
  paid_date: '2026-10-05',
  total: 800,
});
const VOIDED = invoiceRow({
  invoice_number: 'INV-000009',
  status: 'voided',
  voided_at: '2026-10-01T09:00:00',
  void_reason: 'billed two projects together',
  replaced_by: ['INV-000010', 'INV-000011'],
  has_pdf: true,
});

const CLIENTS = [
  {
    key: 'acme',
    name: 'Acme Corp',
    email: '',
    address: '',
    terms: 30,
    entity: 'main',
    entity_name: 'Main',
    schedule: 'on-demand',
    schedule_day: 1,
    ar_account: '',
  },
];
const SERVICES = [
  { key: 'dev', display_name: 'Development', rate: 150, type: 'hours', income_account: '' },
];
const ENTITIES = [{ key: 'main', name: 'Main' }];

type Handler = (body: any) => Response | Promise<Response>;
let posts: { url: string; body: any }[];
let handlers: Record<string, Handler>;
let fetchMock: ReturnType<typeof vi.fn>;
let listed: unknown[];

function preview(over: Record<string, unknown> = {}) {
  return {
    invoice_number: 'INV-000012',
    client_key: 'acme',
    client: 'Acme Corp',
    group: 'Development',
    items: 1,
    total: 450,
    terms: '30',
    entry_uids: ['w1'],
    entry_descriptions: ['API work'],
    ...over,
  };
}

function generated(dryRun: boolean, invoices: unknown[]) {
  return Response.json({
    status: 'ok',
    dry_run: dryRun,
    invoice_count: invoices.length,
    total: invoices.reduce((sum: number, inv: any) => sum + inv.total, 0),
    invoices,
    notifications: 'none',
  });
}

beforeEach(() => {
  posts = [];
  handlers = {};
  listed = [OPEN, PAID, VOIDED];
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace('/istota/api/money', '');
    if (init?.method === 'POST') {
      const body = init.body ? JSON.parse(String(init.body)) : null;
      posts.push({ url: path, body });
      const handler = handlers[path];
      if (!handler) throw new Error(`Unexpected POST: ${path}`);
      return handler(body);
    }
    if (path.startsWith('/invoices?'))
      return Response.json({
        status: 'ok',
        invoices: listed,
        invoice_count: listed.length,
        outstanding_count: 1,
        payment_reviews: [],
      });
    if (path === '/clients') return Response.json({ status: 'ok', clients: CLIENTS });
    if (path === '/business-settings')
      return Response.json({
        status: 'ok',
        entities: ENTITIES,
        services: SERVICES,
        defaults: null,
      });
    if (path.startsWith('/invoice-details'))
      return Response.json({
        status: 'ok',
        invoice_number: 'INV-000010',
        items: [
          {
            description: 'Development',
            detail: 'API work',
            quantity: 10,
            rate: 150,
            discount: 0,
            amount: 1500,
          },
        ],
      });
    throw new Error(`Unexpected request: ${path}`);
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  cleanup();
  viewer.close();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// bits-ui opens the menu on pointerdown, which jsdom only partly implements;
// the keyboard path is equivalent (see KebabMenu.svelte.test.ts).
async function openMenu(number: string) {
  await fireEvent.keyDown(await screen.findByLabelText(`Actions for ${number}`), { key: 'Enter' });
}

async function menuLabels(number: string): Promise<string[]> {
  await openMenu(number);
  const items = await screen.findAllByRole('menuitem');
  const labels = items.map((item) => (item.textContent ?? '').trim());
  await fireEvent.keyDown(items[0], { key: 'Escape' });
  return labels;
}

async function choose(number: string, label: string) {
  await openMenu(number);
  await fireEvent.click(await screen.findByRole('menuitem', { name: label }));
}

async function pick(label: string, optionName: string) {
  await fireEvent.keyDown(screen.getByRole('button', { name: label }), { key: 'Enter' });
  const option = await screen.findByRole('option', { name: optionName });
  await fireEvent.pointerDown(option, { pointerType: 'mouse' });
  await fireEvent.pointerUp(option, { pointerType: 'mouse' });
  await fireEvent.click(option);
}

describe('voided rows', () => {
  it('render a Voided badge with the void record in its tooltip', async () => {
    render(Page);
    const badge = await screen.findByText('Voided');
    const holder = badge.closest('[title]') as HTMLElement;
    expect(holder.title).toContain('billed two projects together');
    expect(holder.title).toContain('INV-000010, INV-000011');
  });

  it('offer no pay, pending or void action, and keep Download PDF', async () => {
    render(Page);
    expect(await menuLabels('INV-000009')).toEqual(['Preview', 'Download PDF']);
  });

  it('expand to list the work the voided invoice billed', async () => {
    render(Page);
    const row = (await screen.findByText('INV-000009')).closest('[aria-expanded]') as HTMLElement;
    await fireEvent.click(row);
    expect(await screen.findByText('API work')).toBeTruthy();
    expect(row).toHaveAttribute('aria-expanded', 'true');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/invoice-details?invoice_number=INV-000009'),
      expect.anything(),
    );
  });

  it('leave an open invoice its pay and void actions', async () => {
    render(Page);
    expect(await menuLabels('INV-000010')).toEqual([
      'Mark paid',
      'Preview',
      'Download PDF',
      'Void invoice…',
    ]);
  });
});

describe('PDF preview', () => {
  const url = '/istota/api/money/invoices/INV-000010/pdf';

  it('opens the invoice PDF in the file viewer', async () => {
    const open = vi.spyOn(viewer, 'openFile');
    render(Page);
    await choose('INV-000010', 'Preview');
    expect(open).toHaveBeenCalledExactlyOnceWith(url, {
      kind: 'url',
      url,
      name: 'INV-000010',
      mime: 'application/pdf',
    });
  });

  it('is disabled when the invoice has no PDF', async () => {
    listed = [invoiceRow({ has_pdf: false })];
    const open = vi.spyOn(viewer, 'openFile');
    render(Page);
    await openMenu('INV-000010');
    const item = await screen.findByRole('menuitem', { name: 'Preview' });
    expect(item).toHaveAttribute('data-disabled');
    await fireEvent.click(item);
    expect(open).not.toHaveBeenCalled();
  });

  it('disables Download PDF on an open invoice with no PDF', async () => {
    listed = [invoiceRow({ has_pdf: false })];
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(Page);
    await openMenu('INV-000010');
    const item = await screen.findByRole('menuitem', { name: 'Download PDF' });
    expect(item).toHaveAttribute('data-disabled');
    await fireEvent.click(item);
    expect(open).not.toHaveBeenCalled();
  });

  it('enables Download PDF when the invoice has one', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(Page);
    await choose('INV-000010', 'Download PDF');
    expect(open).toHaveBeenCalledWith(url, '_blank');
  });

  it('returns focus to the row kebab when the viewer closes', async () => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    render(Page);
    render(FileViewerHost);
    await choose('INV-000010', 'Preview');
    const dialog = await screen.findByRole('dialog', { name: 'INV-000010' });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByLabelText('Actions for INV-000010')),
    );
  });
});

describe('the void dialog', () => {
  it('lists the released work and sends the reason and replacements', async () => {
    handlers['/invoices/INV-000010/void'] = () =>
      Response.json({ status: 'ok', invoice: VOIDED, entries_released: 1 });
    render(Page);
    await choose('INV-000010', 'Void invoice…');
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(/API work/)).toBeTruthy();

    await fireEvent.input(within(dialog).getByLabelText('Reason'), {
      target: { value: 'wrong rate' },
    });
    await fireEvent.input(within(dialog).getByLabelText(/^Replaced by/), {
      target: { value: 'INV-000012, INV-000013' },
    });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Void invoice' }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].body).toEqual({
      reason: 'wrong rate',
      replaced_by: ['INV-000012', 'INV-000013'],
      force: false,
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('asks a paid invoice for the second checkbox before it sends force', async () => {
    handlers['/invoices/INV-000011/void'] = () =>
      Response.json({ status: 'ok', invoice: VOIDED, entries_released: 1 });
    render(Page);
    await choose('INV-000011', 'Void invoice…');
    const dialog = await screen.findByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Void invoice' });
    expect(confirm).toBeDisabled();

    await fireEvent.click(within(dialog).getByLabelText('This invoice was paid. Void anyway'));
    expect(confirm).toBeEnabled();
    await fireEvent.click(confirm);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].body.force).toBe(true);
  });

  it('keeps the dialog open on a 409 and offers the checkbox when the server says paid', async () => {
    handlers['/invoices/INV-000010/void'] = (body) =>
      body.force
        ? Response.json({ status: 'ok', invoice: VOIDED, entries_released: 1 })
        : Response.json(
            { status: 'error', error: 'INV-000010 is paid', code: 'paid' },
            { status: 409 },
          );
    render(Page);
    await choose('INV-000010', 'Void invoice…');
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Void invoice' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('INV-000010 is paid');
    const confirm = within(dialog).getByRole('button', { name: 'Void invoice' });
    expect(confirm).toBeDisabled();
    await fireEvent.click(within(dialog).getByLabelText('This invoice was paid. Void anyway'));
    await fireEvent.click(confirm);
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].body.force).toBe(true);
  });

  it('shows an already-voided refusal without closing', async () => {
    handlers['/invoices/INV-000010/void'] = () =>
      Response.json(
        {
          status: 'error',
          error: 'INV-000010 already voided on 2026-10-01',
          code: 'already_voided',
        },
        { status: 409 },
      );
    render(Page);
    await choose('INV-000010', 'Void invoice…');
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Void invoice' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('already voided');
    expect(within(dialog).queryByLabelText('This invoice was paid. Void anyway')).toBeNull();
  });
});

describe('the generate dialog', () => {
  it('previews with the filters, says no email is sent, then issues the same request', async () => {
    handlers['/invoices/generate'] = (body) => generated(body.dry_run, [preview()]);
    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'Generate invoices' }));
    const dialog = await screen.findByRole('dialog');
    await pick('Client', 'Acme Corp');
    await fireEvent.input(within(dialog).getByLabelText(/^Period/), {
      target: { value: '2026-09' },
    });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Preview' }));

    expect(await within(dialog).findByText('INV-000012')).toBeTruthy();
    expect(within(dialog).getByText('No email is sent to clients.')).toBeTruthy();
    expect(within(dialog).getByText('API work')).toBeTruthy();
    expect(posts[0].body).toEqual({ client: 'acme', period: '2026-09', dry_run: true });

    await fireEvent.click(within(dialog).getByRole('button', { name: 'Issue 1 invoice' }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].body).toEqual({ client: 'acme', period: '2026-09', dry_run: false });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('disables the confirm when the preview has nothing to bill', async () => {
    handlers['/invoices/generate'] = (body) => generated(body.dry_run, []);
    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'Generate invoices' }));
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Preview' }));
    expect(await within(dialog).findByText(/Nothing to invoice/)).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: /^Issue/ })).toBeDisabled();
  });

  it('reports a result that differs from the preview instead of closing', async () => {
    handlers['/invoices/generate'] = (body) =>
      body.dry_run
        ? generated(true, [preview()])
        : generated(false, [preview(), preview({ invoice_number: 'INV-000013', total: 90 })]);
    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'Generate invoices' }));
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Preview' }));
    await fireEvent.click(await within(dialog).findByRole('button', { name: 'Issue 1 invoice' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/differ from the preview/);
    expect(within(dialog).getByText('INV-000013')).toBeTruthy();
    expect(posts).toHaveLength(2);
  });
});

describe('the manual invoice dialog', () => {
  it('sends the client and a manual amount line', async () => {
    handlers['/invoices'] = () =>
      Response.json({ status: 'ok', invoice: invoiceRow({ invoice_number: 'INV-000012' }) });
    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'New manual invoice' }));
    const dialog = await screen.findByRole('dialog');
    const create = within(dialog).getByRole('button', { name: 'Create invoice' });
    expect(create).toBeDisabled();

    await pick('Client', 'Acme Corp');
    await fireEvent.input(within(dialog).getByLabelText('Description'), {
      target: { value: 'Hosting' },
    });
    await fireEvent.input(within(dialog).getByLabelText('Amount'), { target: { value: '120' } });
    await fireEvent.click(create);

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({
      url: '/invoices',
      body: { client: 'acme', items: [{ description: 'Hosting', amount: 120 }] },
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});
