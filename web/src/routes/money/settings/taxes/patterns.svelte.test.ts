import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { settingsSave } from '$lib/stores/settingsSave.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('@minot/ui/stores/notices', () => ({ notifyError: vi.fn(), notifySuccess: vi.fn() }));
import Page from './+page.svelte';

const PROBLEM = 'Add at least one pattern, with no blank rows.';

let patterns = { se_income: ['Income:Consulting'], se_expense: ['Expenses:Business'] };
let failSave = false;
let failReload = false;
let failTotals = false;
const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
  const path = url.split('?')[0];
  if (path.endsWith('/config/tax/patterns')) {
    if (init?.method === 'PUT') {
      if (failSave)
        return new Response(JSON.stringify({ error: 'Could not save' }), { status: 400 });
      patterns = JSON.parse(init.body as string);
    }
    if (failReload && !init?.method)
      return new Response(JSON.stringify({ error: 'Could not load' }), { status: 500 });
    return Response.json({ status: 'ok', patterns });
  }
  if (path.endsWith('/config/tax'))
    return Response.json({ tax: { tax_year: 2026, filing_status: 'single', state: '' } });
  if (path.endsWith('/config/tax/jurisdictions')) return Response.json({ jurisdictions: [] });
  if (path.endsWith('/config/tax/resolved')) return Response.json(null);
  if (path.endsWith('/accounts') && url.includes('year=')) {
    if (failTotals) return new Response(JSON.stringify({ error: 'No ledger' }), { status: 404 });
    return Response.json({
      accounts: [
        { account: 'Income:Consulting', 'sum(position)': '-900.00 USD' },
        { account: 'Income:Interest', 'sum(position)': '-100.00 USD' },
        { account: 'Expenses:Business', 'sum(position)': '50.00 USD' },
        { account: 'Assets:Bank', 'sum(position)': '850.00 USD' },
      ],
    });
  }
  if (path.endsWith('/accounts'))
    return Response.json({
      accounts: [
        { account: 'Income:Consulting', 'sum(position)': '' },
        { account: 'Income:Other', 'sum(position)': '' },
        { account: 'Expenses:Business', 'sum(position)': '' },
        { account: 'Assets:Bank', 'sum(position)': '' },
      ],
    });
  throw new Error(`Unexpected request: ${url}`);
});

beforeEach(() => {
  patterns = { se_income: ['Income:Consulting'], se_expense: ['Expenses:Business'] };
  failSave = false;
  failReload = false;
  failTotals = false;
  fetcher.mockClear();
  vi.stubGlobal('fetch', fetcher);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('loads, adds and removes patterns and saves both lists through one PUT', async () => {
  render(Page);
  expect(await screen.findByDisplayValue('Income:Consulting')).toBeTruthy();
  expect(screen.getByText(/anchored at the start/)).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Add SE income pattern' }));
  const input = screen.getByLabelText('SE income pattern 2');
  await fireEvent.focus(input);
  expect(await screen.findByRole('option', { name: 'Income:Other' })).toBeTruthy();
  expect(screen.queryByRole('option', { name: 'Assets:Bank' })).toBeNull();
  await fireEvent.input(input, { target: { value: 'Income:Other' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Remove SE income pattern 1' }));
  await waitFor(() => expect(get(settingsSave)?.dirty).toBe(true));
  await get(settingsSave)!.save();
  const writes = fetcher.mock.calls.filter(([, init]) => init?.method === 'PUT');
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe('/istota/api/money/config/tax/patterns');
  expect(JSON.parse(writes[0][1]!.body as string)).toEqual({
    se_income: ['Income:Other'],
    se_expense: ['Expenses:Business'],
  });
  await waitFor(() => expect(get(settingsSave)?.dirty).toBe(false));
});

it.each(['income', 'expense'])(
  'blocks an empty SE %s list without losing dirty state',
  async (kind) => {
    render(Page);
    await screen.findByDisplayValue('Income:Consulting');
    await fireEvent.click(screen.getByRole('button', { name: `Remove SE ${kind} pattern 1` }));
    await waitFor(() => expect(get(settingsSave)?.disabled).toBe(true));
    expect(get(settingsSave)?.dirty).toBe(true);
    expect(screen.getByText(PROBLEM)).toBeTruthy();
  },
);

it('saves other tax settings when no patterns are stored', async () => {
  patterns = { se_income: [], se_expense: [] };
  render(Page);
  await screen.findByRole('button', { name: 'Add SE income pattern' });
  await waitFor(() => expect(get(settingsSave)?.disabled).toBe(false));
  expect(screen.getAllByText('None set, so the estimate counts none.')).toHaveLength(2);
  expect(screen.queryByText(PROBLEM)).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Add SE income pattern' }));
  await waitFor(() => expect(get(settingsSave)?.disabled).toBe(true));
  expect(screen.getAllByText(PROBLEM)).toHaveLength(2);
});

it('sends patterns trimmed', async () => {
  render(Page);
  const input = await screen.findByDisplayValue('Income:Consulting');
  await fireEvent.input(input, { target: { value: ' Income:Other ' } });
  await get(settingsSave)!.save();
  expect(patterns.se_income).toEqual(['Income:Other']);
});

it('marks an invalid regular expression beside its field and blocks Save', async () => {
  render(Page);
  const input = await screen.findByDisplayValue('Income:Consulting');
  await fireEvent.input(input, { target: { value: 'Income:[' } });
  await waitFor(() => expect(get(settingsSave)?.disabled).toBe(true));
  expect(screen.getByText('Not a valid regular expression.')).toBeTruthy();
});

it('marks a pattern that matches every account', async () => {
  render(Page);
  const input = await screen.findByDisplayValue('Income:Consulting');
  await fireEvent.input(input, { target: { value: 'Income:Consulting|' } });
  await waitFor(() => expect(get(settingsSave)?.disabled).toBe(true));
  expect(screen.getByText('This matches every account.')).toBeTruthy();
});

it('previews the accounts each list matches, following unsaved edits', async () => {
  render(Page);
  const input = await screen.findByDisplayValue('Income:Consulting');
  const income = await screen.findByRole('list', { name: 'SE income matches' });
  expect(income.textContent).toContain('Income:Consulting');
  expect(income.textContent).toContain('900.00 USD');
  expect(income.textContent).not.toContain('-900.00');
  expect(income.textContent).not.toContain('Income:Interest');
  expect(screen.getByRole('list', { name: 'SE expense matches' }).textContent).toContain(
    '50.00 USD',
  );

  await fireEvent.input(input, { target: { value: 'income' } });
  await waitFor(() =>
    expect(screen.getByRole('list', { name: 'SE income matches' }).textContent).toContain(
      'Income:Interest',
    ),
  );

  await fireEvent.input(input, { target: { value: 'Income:Nothing' } });
  expect(await screen.findByText('Matches no account with postings in 2026.')).toBeTruthy();

  await fireEvent.input(input, { target: { value: 'Income:[' } });
  await waitFor(() => expect(screen.queryByRole('list', { name: 'SE income matches' })).toBeNull());
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(0);
});

it('anchors the preview at the start of the account name', async () => {
  render(Page);
  const input = await screen.findByDisplayValue('Income:Consulting');
  await fireEvent.input(input, { target: { value: 'Consulting' } });
  expect(await screen.findByText('Matches no account with postings in 2026.')).toBeTruthy();
});

it('leaves the preview out when the totals cannot be read', async () => {
  failTotals = true;
  render(Page);
  await screen.findByDisplayValue('Income:Consulting');
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url.includes('year='))).toBe(true));
  expect(screen.queryByRole('list', { name: 'SE income matches' })).toBeNull();
  expect(screen.queryByText(/Matches no account/)).toBeNull();
});

it('can still save after a reload fails', async () => {
  render(Page);
  const input = await screen.findByDisplayValue('Income:Consulting');
  await fireEvent.input(input, { target: { value: 'Income:Other' } });
  failReload = true;
  await get(settingsSave)!.save();
  failReload = false;
  await fireEvent.input(screen.getByDisplayValue('Income:Other'), {
    target: { value: 'Income:Third' },
  });
  await waitFor(() => expect(get(settingsSave)?.disabled).toBe(false));
  await get(settingsSave)!.save();
  expect(patterns.se_income).toEqual(['Income:Third']);
});

it('keeps edits after a failed save so they can be retried', async () => {
  render(Page);
  const input = await screen.findByDisplayValue('Income:Consulting');
  await fireEvent.input(input, { target: { value: 'Income:Other' } });
  failSave = true;
  await expect(get(settingsSave)!.save()).rejects.toThrow('Could not save');
  expect(get(settingsSave)?.dirty).toBe(true);
  expect(screen.getByDisplayValue('Income:Other')).toBeTruthy();
  failSave = false;
  await get(settingsSave)!.save();
  await waitFor(() => expect(get(settingsSave)?.dirty).toBe(false));
});
