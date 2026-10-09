import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import EntityForm from './EntityForm.svelte';
import { updateEntity, getBusinessSettings, type EntityRow } from '$lib/money/api';

let ledgers: string[];
let accounts: Record<string, string[]>;
let saved: EntityRow;
let accountResponse: ((ledger: string) => Promise<Response>) | null;
let ledgerError: boolean;

function response(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status });
}

function accountData(names: string[]) {
  return { status: 'ok', accounts: names.map((account) => ({ account, 'sum(position)': '' })) };
}

beforeEach(() => {
  ledgers = ['Business', 'Personal'];
  accounts = {
    Business: ['Assets:Bank:Checking', 'Income:Consulting', 'Income:Royalties'],
    Personal: ['Income:Consulting'],
  };
  saved = row();
  accountResponse = null;
  ledgerError = false;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'https://app.example.com');
      if (url.pathname.endsWith('/ledgers')) {
        return ledgerError
          ? response({ error: 'Ledgers unavailable' }, 500)
          : response({ ledgers });
      }
      if (url.pathname.endsWith('/accounts')) {
        const ledger = url.searchParams.get('ledger')!;
        expect(url.searchParams.has('year')).toBe(false);
        if (accountResponse) return accountResponse(ledger);
        return response(accountData(accounts[ledger] ?? []));
      }
      if (url.pathname.endsWith('/config/companies/main') && init?.method === 'PUT') {
        saved = { ...saved, ...JSON.parse(init.body as string) };
        return response({ status: 'ok' });
      }
      if (url.pathname.endsWith('/business-settings')) {
        return response({ entities: [saved], services: [], defaults: {} });
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function pick(label: string, optionName: string) {
  await fireEvent.keyDown(screen.getByRole('button', { name: label }), { key: 'Enter' });
  const option = await screen.findByRole('option', { name: optionName, exact: true });
  await fireEvent.pointerDown(option, { pointerType: 'mouse' });
  await fireEvent.pointerUp(option, { pointerType: 'mouse' });
  await fireEvent.click(option);
}

async function enable() {
  await fireEvent.click(
    screen.getByRole('checkbox', { name: 'Automatically detect invoice payments' }),
  );
}

async function ready() {
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Ledger', exact: true })).not.toBeDisabled(),
  );
}

function row(overrides: Partial<EntityRow> = {}): EntityRow {
  return {
    key: 'main',
    name: 'Main LLC',
    address: '1 Main St',
    email: 'billing@main.example',
    payment_instructions: 'Wire to…',
    logo: '',
    ar_account: 'Assets:Accounts-Receivable',
    bank_account: 'Assets:Bank:Checking',
    currency: 'USD',
    payment_detection_enabled: false,
    payment_detection_ledger: '',
    payment_detection_income_account: '',
    ...overrides,
  };
}

function mount(props: Record<string, unknown> = {}) {
  return render(EntityForm, {
    props: { onSave: vi.fn(), onCancel: vi.fn(), ...props } as any,
  });
}

describe('EntityForm', () => {
  it('sends the edited fields under the existing key', async () => {
    const onSave = vi.fn();
    mount({ entity: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('Acme Studio LLC'), {
      target: { value: 'Main Holdings LLC' },
    });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenCalledWith(
      'main',
      expect.objectContaining({ name: 'Main Holdings LLC' }),
    );
  });

  it.each(['/etc/passwd', '../../secrets.png', '~/private.png'])(
    'refuses a logo path that escapes the accounting folder: %s',
    async (logo) => {
      // The logo is base64-embedded into the invoice, resolved against the
      // accounting folder — pathlib lets an absolute operand replace it.
      const onSave = vi.fn();
      mount({ entity: row(), onSave });
      await fireEvent.input(screen.getByPlaceholderText('invoices/logo.png'), {
        target: { value: logo },
      });
      expect(screen.getByText('Expected a path inside the accounting folder')).toBeTruthy();
      await fireEvent.click(screen.getByText('Save'));
      expect(onSave).not.toHaveBeenCalled();
    },
  );

  it('accepts a relative logo path', async () => {
    const onSave = vi.fn();
    mount({ entity: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('invoices/logo.png'), {
      target: { value: 'invoices/logo.png' },
    });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenCalledWith(
      'main',
      expect.objectContaining({ logo: 'invoices/logo.png' }),
    );
  });

  it('rejects a malformed new key before it reaches the server', async () => {
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.input(screen.getByPlaceholderText('main'), {
      target: { value: 'has space' },
    });
    expect(screen.getByText('Letters, digits, - and _ only')).toBeTruthy();
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('will not save without a name', async () => {
    const onSave = vi.fn();
    mount({ entity: row({ name: '' }), onSave });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('treats the key as immutable when editing', () => {
    mount({ entity: row() });
    expect(screen.queryByPlaceholderText('main')).toBeNull();
    expect(screen.getByText('main')).toBeTruthy();
  });

  it('renders a server error', () => {
    mount({ entity: row(), error: "entity 'main' is the default entity" });
    expect(screen.getByText("entity 'main' is the default entity")).toBeTruthy();
  });
});

describe('EntityForm payment detection', () => {
  it('starts disabled and requires an explicit pair, then saves and reloads through the API', async () => {
    const onSave = vi.fn(updateEntity);
    const form = mount({ entity: row(), onSave });
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    await enable();
    await ready();
    expect(screen.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
    await pick('Ledger', 'Business');
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Income account', exact: true }),
      ).not.toBeDisabled(),
    );
    expect(screen.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
    await pick('Income account', 'Income:Royalties');
    await fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(saved.payment_detection_enabled).toBe(true));
    expect(saved).toMatchObject({
      payment_detection_ledger: 'Business',
      payment_detection_income_account: 'Income:Royalties',
      bank_account: 'Assets:Bank:Checking',
    });
    form.unmount();
    const data = await getBusinessSettings();
    mount({ entity: data.entities[0] });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save', exact: true })).not.toBeDisabled(),
    );
    expect(screen.getByRole('checkbox')).toBeChecked();
    expect(screen.getByRole('button', { name: 'Ledger', exact: true })).toHaveTextContent(
      'Business',
    );
    expect(screen.getByRole('button', { name: 'Income account', exact: true })).toHaveTextContent(
      'Income:Royalties',
    );
  });

  it('clears the account on ledger changes and selects a sole declared income account without postings', async () => {
    const onSave = vi.fn();
    mount({
      entity: row({
        payment_detection_enabled: true,
        payment_detection_ledger: 'Business',
        payment_detection_income_account: 'Income:Royalties',
      }),
      onSave,
    });
    await ready();
    await pick('Ledger', 'Personal');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Income account', exact: true })).toHaveTextContent(
        'Income:Consulting',
      ),
    );
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenLastCalledWith(
      'main',
      expect.objectContaining({
        payment_detection_ledger: 'Personal',
        payment_detection_income_account: 'Income:Consulting',
      }),
    );
    await pick('Ledger', 'Business');
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Income account', exact: true }),
      ).not.toBeDisabled(),
    );
    expect(screen.getByRole('button', { name: 'Income account', exact: true })).toHaveTextContent(
      'Choose an income account',
    );
    expect(screen.getByText('Save')).toBeDisabled();
  });

  it('ignores late account successes and failures from a previous ledger', async () => {
    let resolveOld!: (value: Response) => void;
    accountResponse = (ledger) =>
      ledger === 'Business'
        ? new Promise((resolve) => {
            resolveOld = resolve;
          })
        : Promise.resolve(response(accountData(['Income:Consulting'])));
    mount({ entity: row({ payment_detection_enabled: true }) });
    await ready();
    await pick('Ledger', 'Business');
    expect(screen.getByText('Loading income accounts…')).toBeTruthy();
    expect(screen.getByText('Save')).toBeDisabled();
    await pick('Ledger', 'Personal');
    await waitFor(() => expect(screen.getByText('Save')).not.toBeDisabled());
    resolveOld(response({ error: 'Old ledger failed' }, 500));
    await waitFor(() => expect(screen.queryByText('Loading income accounts…')).toBeNull());
    await pick('Ledger', 'Business');
    await pick('Ledger', 'Personal');
    await waitFor(() => expect(screen.getByText('Save')).not.toBeDisabled());
    resolveOld(response(accountData(['Income:Wrong'])));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('Old ledger failed')).toBeNull();
    expect(screen.getByRole('button', { name: 'Income account', exact: true })).toHaveTextContent(
      'Income:Consulting',
    );
  });

  it.each(['ledger', 'accounts', 'empty'])(
    'blocks enabled saving on %s failure but allows disabling with saved choices intact',
    async (failure) => {
      if (failure === 'ledger') ledgerError = true;
      if (failure === 'accounts')
        accountResponse = async () => response({ error: 'Accounts unavailable' }, 500);
      if (failure === 'empty') accounts.Business = ['Assets:Bank:Checking'];
      const onSave = vi.fn();
      mount({
        entity: row({
          payment_detection_enabled: true,
          payment_detection_ledger: 'Business',
          payment_detection_income_account: 'Income:Old',
        }),
        onSave,
      });
      const message =
        failure === 'ledger'
          ? 'Ledgers unavailable'
          : failure === 'accounts'
            ? 'Accounts unavailable'
            : 'No income accounts in this ledger.';
      await screen.findByText(message);
      expect(screen.getByText('Save')).toBeDisabled();
      await enable();
      await fireEvent.click(screen.getByText('Save'));
      expect(onSave).toHaveBeenCalledWith(
        'main',
        expect.objectContaining({
          payment_detection_enabled: false,
          payment_detection_ledger: 'Business',
          payment_detection_income_account: 'Income:Old',
        }),
      );
    },
  );

  it.each(['ledger', 'account'])(
    'preserves a missing saved %s so it can be disabled without replacement',
    async (missing) => {
      if (missing === 'ledger') ledgers = ['Personal'];
      else accounts.Business = ['Income:Replacement'];
      const onSave = vi.fn();
      mount({
        entity: row({
          payment_detection_enabled: true,
          payment_detection_ledger: 'Business',
          payment_detection_income_account: 'Income:Old',
        }),
        onSave,
      });
      await screen.findByText(
        missing === 'ledger'
          ? 'Saved ledger is unavailable. Choose another ledger or turn detection off.'
          : 'Saved income account is unavailable. Choose another account or turn detection off.',
      );
      expect(screen.getByText('Save')).toBeDisabled();
      await enable();
      await fireEvent.click(screen.getByText('Save'));
      expect(onSave).toHaveBeenCalledWith(
        'main',
        expect.objectContaining({
          payment_detection_enabled: false,
          payment_detection_ledger: 'Business',
          payment_detection_income_account: 'Income:Old',
        }),
      );
    },
  );

  it('reports no configured ledgers and lets the form remain disabled', async () => {
    ledgers = [];
    mount({ entity: row() });
    await enable();
    await screen.findByText('No ledgers configured.');
    expect(screen.getByText('Save')).toBeDisabled();
    await enable();
    expect(screen.getByText('Save')).not.toBeDisabled();
  });

  it('keeps saved choices when switched off and back on, and does not save on checkbox Enter', async () => {
    const onSave = vi.fn();
    mount({
      entity: row({
        payment_detection_enabled: true,
        payment_detection_ledger: 'Business',
        payment_detection_income_account: 'Income:Royalties',
      }),
      onSave,
    });
    await waitFor(() => expect(screen.getByText('Save')).not.toBeDisabled());
    await fireEvent.keyDown(screen.getByRole('checkbox'), { key: 'Enter' });
    expect(onSave).not.toHaveBeenCalled();
    await enable();
    await enable();
    await waitFor(() => expect(screen.getByText('Save')).not.toBeDisabled());
    expect(screen.getByRole('button', { name: 'Income account', exact: true })).toHaveTextContent(
      'Income:Royalties',
    );
  });
});
