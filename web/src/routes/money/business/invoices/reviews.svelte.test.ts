import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import Page from './+page.svelte';
import { formatDate } from '$lib/dateFormat';

vi.mock('$app/paths', () => ({ base: '/istota' }));

const ident = 'a'.repeat(32);
const review = {
  ledger_txn_id: ident,
  txn_date: '2026-04-15',
  amount: 1200,
  payee: '<b>Acme payment</b>',
  account: 'Assets:Bank:Checking',
  candidate_details: [1, 2, 3, 4].map((n) => ({
    invoice_number: `INV-00000${n}`,
    client: 'acme',
    total: 1200,
  })),
};
const invoice = {
  invoice_number: 'INV-000005',
  client: 'Acme Corp',
  client_key: 'acme',
  date: '2026-03-02',
  total: 1200,
  status: 'paid',
  paid_date: '2026-04-15',
  paid_by_sync_date: '2026-04-15',
};
let reviews = [review];
let actionResponse: () => Promise<Response>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  reviews = [review];
  actionResponse = async () => {
    reviews = [];
    return Response.json({ status: 'ok' });
  };
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return actionResponse();
    if (url.includes('/invoices?'))
      return Response.json({
        status: 'ok',
        invoices: [invoice],
        invoice_count: 1,
        outstanding_count: 0,
        payment_reviews: reviews,
      });
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it.each(['settle', 'dismiss'])(
  'posts %s through the money API and refreshes the review strip',
  async (action) => {
    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: /Payments to review \(1\)/ }));
    expect(screen.getByText('<b>Acme payment</b>', { exact: false }).querySelector('b')).toBeNull();
    expect(screen.getByText('Assets:Bank:Checking')).toBeTruthy();
    expect(screen.getByText(`paid by sync on ${formatDate('2026-04-15')}`)).toBeTruthy();
    const button = screen.getByRole('button', {
      name: action === 'settle' ? /Settle INV-000004/ : 'Not an invoice payment',
    });
    await fireEvent.click(button);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/istota/api/money/invoices/review/${ident}/${action}`,
        expect.objectContaining({
          method: 'POST',
          credentials: 'same-origin',
          body: action === 'settle' ? JSON.stringify({ invoice_number: 'INV-000004' }) : '{}',
        }),
      ),
    );
    await waitFor(() => expect(screen.queryByText(/Payments to review/)).toBeNull());
  },
);

it('keeps the review and error visible after rejection and disables actions while pending', async () => {
  let finish!: (response: Response) => void;
  actionResponse = () =>
    new Promise((resolve) => {
      finish = resolve;
    });
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: /Payments to review/ }));
  await fireEvent.click(screen.getByRole('button', { name: /Settle INV-000001/ }));
  expect(screen.getByRole('button', { name: 'Not an invoice payment' })).toBeDisabled();
  expect(screen.getByRole('button', { name: /Settle INV-000004/ })).toBeDisabled();
  finish(Response.json({ error: 'Invoice is no longer unpaid' }, { status: 400 }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Invoice is no longer unpaid');
  expect(screen.getByRole('button', { name: /Settle INV-000004/ })).toBeEnabled();
  expect(screen.getByText('Payments to review (1)')).toBeTruthy();
});

it('omits the strip for an empty review list', async () => {
  reviews = [];
  render(Page);
  await screen.findByText('INV-000005');
  expect(screen.queryByText(/Payments to review/)).toBeNull();
});
