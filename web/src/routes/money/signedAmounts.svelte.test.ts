import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/money/api', async (original) => ({
  ...(await original<typeof import('$lib/money/api')>()),
  getAccounts: vi.fn(),
  getReport: vi.fn(),
  getPortfolioSummary: vi.fn(),
  getInvoices: vi.fn(),
  getInvoiceDetails: vi.fn(),
}));

import {
  getAccounts,
  getInvoiceDetails,
  getInvoices,
  getPortfolioSummary,
  getReport,
} from '$lib/money/api';
import { selectedLedger } from '$lib/money/stores/ledger';
import { selectedYear } from '$lib/money/stores/transactions';
import AccountsPage from './accounts/+page.svelte';
import IncomeStatementPage from './reports/income-statement/+page.svelte';
import PortfolioOverviewPage from './portfolio/overview/+page.svelte';
import InvoicesPage from './business/invoices/+page.svelte';

function amount(text: string): HTMLElement {
  const node = screen.getByText(text);
  return (node.closest('[data-direction]') as HTMLElement | null) ?? node;
}

beforeEach(() => {
  vi.clearAllMocks();
  selectedLedger.set('');
  selectedYear.set(2026);
});
afterEach(cleanup);

describe('ledger amounts', () => {
  it('tints Income and Expenses accounts by kind and leaves the rest plain', async () => {
    vi.mocked(getAccounts).mockResolvedValue({
      status: 'ok',
      accounts: [
        { account: 'Income:Salary', 'sum(position)': '-1000.00 USD' },
        { account: 'Expenses:Rent', 'sum(position)': '-25.00 USD' },
        { account: 'Assets:Checking', 'sum(position)': '-5.00 USD' },
      ],
    });
    render(AccountsPage);
    await screen.findByText('1000.00 USD');
    // A refund is still an expense: the account decides, not the sign.
    expect(amount('1000.00 USD').dataset.direction).toBe('positive');
    expect(amount('1000.00 USD').style.color).toBe('var(--signed-pos)');
    expect(amount('-25.00 USD').dataset.direction).toBe('negative');
    expect(amount('-25.00 USD').style.color).toBe('var(--signed-neg)');
    expect(amount('-5.00 USD').dataset.direction).toBeUndefined();
    expect(amount('-5.00 USD').style.color).toBe('');
  });
});

describe('report amounts', () => {
  it('tints the section totals by kind and the net by its sign', async () => {
    vi.mocked(getReport).mockResolvedValue({
      status: 'ok',
      report_type: 'income-statement',
      year: 2026,
      row_count: 2,
      results: [
        { account: 'Income:Salary', 'sum(position)': '-1000.00 USD' },
        { account: 'Expenses:Rent', 'sum(position)': '1500.00 USD' },
      ],
    });
    render(IncomeStatementPage);
    await screen.findByText('-500.00 USD');
    expect(amount('1,000.00 USD').dataset.direction).toBe('positive');
    expect(amount('1,500.00 USD').dataset.direction).toBe('negative');
    expect(amount('-500.00 USD').dataset.direction).toBe('negative');
  });
});

describe('portfolio amounts', () => {
  it('tints a gain and a loss, and the unrealized total', async () => {
    const holding = {
      description: '',
      quantity: 1,
      value: 100,
      cost_basis: 50,
      asset_class: 'Equity',
      sub_class: 'Unclassified',
      geography: 'US',
      accounts: 1,
    };
    vi.mocked(getPortfolioSummary).mockResolvedValue({
      status: 'ok',
      summary: {
        snapshot_id: 1,
        exported_at: '2026-01-01T00:00:00Z',
        exported_at_estimated: false,
        total_value: 200,
        position_count: 2,
        by_asset_class: [],
        by_account: [],
        by_account_type: [],
        by_group: [],
        by_geography: [],
        holdings: [
          { ...holding, symbol: 'AAA', gain: 50, gain_pct: 0.1 },
          { ...holding, symbol: 'BBB', gain: -20, gain_pct: -0.05 },
        ],
      },
    });
    render(PortfolioOverviewPage);
    await screen.findByText('$50.00');
    expect(amount('$50.00').dataset.direction).toBe('positive');
    expect(amount('+10.0%').dataset.direction).toBe('positive');
    expect(amount('-$20.00').dataset.direction).toBe('negative');
    expect(amount('-5.0%').dataset.direction).toBe('negative');
    const tile = screen.getByText('Unrealized P&L').parentElement as HTMLElement;
    expect(tile.style.getPropertyValue('--stat-value-fg')).toBe('var(--signed-pos)');
  });
});

describe('invoice amounts', () => {
  it('shows a line discount as a negative amount', async () => {
    vi.mocked(getInvoices).mockResolvedValue({
      status: 'ok',
      invoice_count: 1,
      outstanding_count: 0,
      payment_reviews: [],
      invoices: [
        {
          invoice_number: 'INV-000001',
          client: 'Example Co',
          client_key: 'example',
          date: '2026-03-02',
          total: 90,
          status: 'outstanding',
        },
      ],
    } as never);
    vi.mocked(getInvoiceDetails).mockResolvedValue({
      status: 'ok',
      invoice_number: 'INV-000001',
      items: [
        { description: 'Work', detail: '', quantity: 1, rate: 100, discount: 10, amount: 90 },
      ],
    });
    render(InvoicesPage);
    const row = (await screen.findByText('INV-000001')).closest('.record-row') as HTMLElement;
    await fireEvent.click(row);
    await waitFor(() => expect(screen.getByText('-$10.00')).toBeTruthy());
    expect(amount('-$10.00').dataset.direction).toBe('negative');
  });
});
