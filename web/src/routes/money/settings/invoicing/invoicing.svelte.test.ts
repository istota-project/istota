import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import Page from './+page.svelte';

vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

const notices = vi.hoisted(() => ({ notifyError: vi.fn() }));
vi.mock('$lib/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/stores/notices')>()),
  notifyError: notices.notifyError,
}));

const ENTITY = {
  key: 'main',
  name: 'Main LLC',
  address: '',
  email: '',
  payment_instructions: '',
  logo: '',
  ar_account: '',
  bank_account: '',
  currency: 'USD',
  payment_detection_enabled: false,
  payment_detection_ledger: '',
  payment_detection_income_account: '',
};

// `/clients` resolves the default entity into `entity`; `/config/clients` keeps
// the raw `''` that means "fall back to default_entity".
const DISPLAY = {
  key: 'acme',
  name: 'Acme Corp',
  email: 'ap@acme.example',
  address: '100 Acme Way',
  terms: 30,
  entity: 'main',
  entity_name: 'Main LLC',
  schedule: 'monthly',
  schedule_day: 15,
  ar_account: 'Assets:AR:Acme',
};
const RAW = {
  key: 'acme',
  name: 'Acme Corp',
  email: 'ap@acme.example',
  address: '100 Acme Way',
  terms: 30,
  ar_account: '',
  entity: '',
  schedule: 'monthly',
  schedule_day: 15,
  reminder_days: 3,
  notifications: '',
  days_until_overdue: 0,
  ledger_posting: true,
  bundles: [],
  separate: [],
};
const SERVICE = {
  key: 'consulting',
  display_name: 'Consulting',
  type: 'hours',
  rate: 150,
  income_account: 'Income:Consulting',
};
const DEFAULTS = {
  currency: 'USD',
  default_entity: 'main',
  default_ar_account: 'Assets:AR',
  default_bank_account: 'Assets:Bank',
  invoice_output: 'invoices',
  next_invoice_number: 12,
  days_until_overdue: 0,
  notifications: '',
};

let requests: { method: string; path: string; body: any }[];
let defaults: typeof DEFAULTS | null;
let clients: (typeof DISPLAY)[];
let configs: (typeof RAW)[];
let workEntries: unknown[];
let deleteResponse: () => Response;
let businessFails: boolean;
let clientsFail: boolean;

beforeEach(() => {
  requests = [];
  defaults = DEFAULTS;
  clients = [DISPLAY];
  configs = [RAW];
  workEntries = [];
  deleteResponse = () => Response.json({ status: 'ok' });
  businessFails = false;
  clientsFail = false;
  notices.notifyError.mockClear();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace('/istota/api/money', '');
      const method = init?.method ?? 'GET';
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      requests.push({ method, path, body });
      if (method === 'DELETE') return deleteResponse();
      if (method === 'POST' || method === 'PUT')
        return Response.json({ status: 'ok', client: { ...RAW, ...body } });
      if (path === '/business-settings') {
        if (businessFails)
          return Response.json({ status: 'error', error: 'ledger unreadable' }, { status: 500 });
        return Response.json({ status: 'ok', entities: [ENTITY], services: [SERVICE], defaults });
      }
      if (path === '/clients') {
        if (clientsFail)
          return Response.json({ status: 'error', error: 'clients unreadable' }, { status: 500 });
        return Response.json({ status: 'ok', clients });
      }
      if (path === '/config/clients') return Response.json({ status: 'ok', clients: configs });
      if (path.startsWith('/work?'))
        return Response.json({ status: 'ok', entries: workEntries, totals: {} });
      throw new Error(`Unexpected request: ${method} ${path}`);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function clientsCard(): Promise<HTMLElement> {
  const heading = await screen.findByText(/^Clients \(\d+\)$/);
  return heading.closest('.ui-card') as HTMLElement;
}

async function choose(label: string) {
  const card = await clientsCard();
  await fireEvent.keyDown(within(card).getByLabelText('Client actions'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: label }));
}

describe('the Clients card on Invoicing', () => {
  it('lists clients as a table row with the resolved entity', async () => {
    render(Page);
    const card = await clientsCard();
    const row = within(card).getByText('Acme Corp').closest('tr') as HTMLElement;
    expect(row).toBeTruthy();
    expect(within(row).getByText('acme')).toBeTruthy();
    expect(within(row).getByText('Main LLC')).toBeTruthy();
    expect(within(row).getByText('Net 30')).toBeTruthy();
    expect(within(row).getByText('monthly, day 15')).toBeTruthy();
  });

  it('can add the first client with no invoicing configuration yet', async () => {
    defaults = null;
    clients = [];
    configs = [];
    render(Page);
    const card = await clientsCard();
    expect(within(card).getByText(/No clients yet/)).toBeTruthy();

    await fireEvent.click(within(card).getByRole('button', { name: 'Add client' }));
    const dialog = await screen.findByRole('dialog');
    await fireEvent.input(within(dialog).getByPlaceholderText('acme'), {
      target: { value: 'globex' },
    });
    await fireEvent.input(within(dialog).getByLabelText('Name'), {
      target: { value: 'Globex' },
    });
    await fireEvent.click(within(dialog).getByText('Save'));
    await waitFor(() =>
      expect(requests.some((r) => r.method === 'POST' && r.path === '/config/clients')).toBe(true),
    );
    const post = requests.find((r) => r.method === 'POST')!;
    expect(post.body.key).toBe('globex');
  });

  it('edits the raw record, so the default entity is not materialised', async () => {
    render(Page);
    await choose('Edit');
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Edit Acme Corp')).toBeTruthy();
    await fireEvent.click(within(dialog).getByText('Save'));

    await waitFor(() => expect(requests.some((r) => r.method === 'PUT')).toBe(true));
    const put = requests.find((r) => r.method === 'PUT')!;
    expect(put.path).toBe('/config/clients/acme?create=false');
    expect(put.body.entity).toBe('');
    expect(put.body.ar_account).toBe('');
  });

  it('counts the work entries that reference a client before deleting it', async () => {
    workEntries = [{ uid: 'w1' }, { uid: 'w2' }];
    render(Page);
    await choose('Delete');
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog).toHaveTextContent('2 work entries reference this client'));
    expect(requests.some((r) => r.path === '/work?client=acme&status=all')).toBe(true);

    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() =>
      expect(requests.some((r) => r.method === 'DELETE' && r.path === '/config/clients/acme')).toBe(
        true,
      ),
    );
  });

  it('shows a refused delete as a notice on the card', async () => {
    deleteResponse = () =>
      Response.json({ status: 'error', error: 'client is referenced by INV-1' }, { status: 409 });
    render(Page);
    await choose('Delete');
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    const card = await clientsCard();
    expect(await within(card).findByText('client is referenced by INV-1')).toBeTruthy();
  });

  it('reports a delete that fails for another reason as a notice, keeping the list', async () => {
    deleteResponse = () =>
      Response.json({ status: 'error', error: 'database is locked' }, { status: 500 });
    render(Page);
    await choose('Delete');
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('database is locked'));
    const card = await clientsCard();
    expect(within(card).queryByText('database is locked')).toBeNull();
    expect(within(card).getByText('Acme Corp')).toBeTruthy();
  });

  it('shows a failed load in the card with a Retry that reloads it', async () => {
    clientsFail = true;
    render(Page);
    const heading = await screen.findByText('Clients');
    const card = heading.closest('.ui-card') as HTMLElement;
    expect(await within(card).findByRole('alert')).toHaveTextContent('clients unreadable');
    expect(within(card).queryByText(/No clients yet/)).toBeNull();

    clientsFail = false;
    await fireEvent.click(within(card).getByRole('button', { name: 'Retry' }));
    expect(await within(await clientsCard()).findByText('Acme Corp')).toBeTruthy();
  });

  it('re-reads the clients after an entity rename, since rows carry its name', async () => {
    render(Page);
    const card = await clientsCard();
    expect(within(card).getByText('Main LLC')).toBeTruthy();

    clients = [{ ...DISPLAY, entity_name: 'Main Holdings' }];
    await fireEvent.keyDown(screen.getByLabelText('Entity actions'), { key: 'Enter' });
    await fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }));
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByText('Save'));

    expect(await within(await clientsCard()).findByText('Main Holdings')).toBeTruthy();
  });
});

async function cardTitled(title: RegExp): Promise<HTMLElement> {
  const heading = await screen.findByText(title);
  return heading.closest('.ui-card') as HTMLElement;
}

async function deleteFrom(card: HTMLElement, menuLabel: string) {
  await fireEvent.keyDown(within(card).getByLabelText(menuLabel), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
  const dialog = await screen.findByRole('dialog');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
}

describe('the entity and service cards on Invoicing', () => {
  it('shows a failed load in each card with a Retry, not an empty list', async () => {
    businessFails = true;
    render(Page);
    for (const title of [/^Business defaults$/, /^Entities$/, /^Services$/]) {
      const card = await cardTitled(title);
      expect(await within(card).findByRole('alert')).toHaveTextContent('ledger unreadable');
      expect(within(card).getByRole('button', { name: 'Retry' })).toBeTruthy();
    }
    expect(screen.queryByText(/No entities yet/)).toBeNull();
    expect(screen.queryByText(/No services yet/)).toBeNull();

    businessFails = false;
    const entities = await cardTitled(/^Entities$/);
    await fireEvent.click(within(entities).getByRole('button', { name: 'Retry' }));
    expect(await within(await cardTitled(/^Entities \(1\)$/)).findByText('Main LLC')).toBeTruthy();
    expect(within(await cardTitled(/^Services \(1\)$/)).getByText('Consulting')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  for (const [kind, title, menu, row] of [
    ['entity', /^Entities \(1\)$/, 'Entity actions', 'Main LLC'],
    ['service', /^Services \(1\)$/, 'Service actions', 'Consulting'],
  ] as const) {
    it(`reports a failed ${kind} delete as a notice and leaves the cards loaded`, async () => {
      deleteResponse = () =>
        Response.json({ status: 'error', error: 'database is locked' }, { status: 500 });
      render(Page);
      const card = await cardTitled(title);
      await deleteFrom(card, menu);

      await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('database is locked'));
      expect(within(await cardTitled(title)).getByText(row)).toBeTruthy();
      expect(screen.queryByRole('alert')).toBeNull();
      expect(screen.queryByText('database is locked')).toBeNull();
      expect(
        within(await cardTitled(/^Business defaults$/)).getByText('Next invoice #'),
      ).toBeTruthy();
    });
  }

  for (const [kind, title, menu] of [
    ['entity', /^Entities \(1\)$/, 'Entity actions'],
    ['service', /^Services \(1\)$/, 'Service actions'],
  ] as const) {
    it(`keeps a refused ${kind} delete on the card, since it names what to fix`, async () => {
      deleteResponse = () =>
        Response.json({ status: 'error', error: `${kind} is in use` }, { status: 409 });
      render(Page);
      await deleteFrom(await cardTitled(title), menu);
      const card = await cardTitled(title);
      expect(await within(card).findByText(`${kind} is in use`)).toBeTruthy();
      expect(notices.notifyError).not.toHaveBeenCalled();
    });
  }
});
