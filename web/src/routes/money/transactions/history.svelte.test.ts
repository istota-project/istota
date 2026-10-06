import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import { __history, goto } from '$app/navigation';
import { page } from '$app/state';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$app/navigation', async (original) => ({
  ...(await original<typeof import('$app/navigation')>()),
  goto: vi.fn(async () => undefined),
}));

vi.mock('$lib/money/api', async (original) => ({
  ...(await original<typeof import('$lib/money/api')>()),
  getAccounts: vi.fn(),
  getTransactions: vi.fn(),
  getPostings: vi.fn(),
}));

import { getAccounts, getTransactions, getPostings } from '$lib/money/api';
import { selectedAccount, selectedYear, filterText } from '$lib/money/stores/transactions';
import { selectedLedger } from '$lib/money/stores/ledger';
import type { User } from '$lib/api';
import Harness from '$lib/currentUserHarness.test.svelte';
import Layout from './+layout.svelte';
import Page from './+page.svelte';
import AccountsPage from '../accounts/+page.svelte';

const currentUrl = () => __history.entries[__history.index].url;
const mount = () =>
  render(Harness, { layout: Layout, component: Page, user: { username: 'alice' } as User });

beforeEach(() => {
  vi.clearAllMocks();
  __history.reset('/istota/money/transactions/');
  selectedAccount.set('');
  selectedYear.set(2026);
  selectedLedger.set('');
  filterText.set('');
  vi.mocked(getAccounts).mockResolvedValue({
    status: 'ok',
    accounts: [
      { account: 'Assets:Checking', 'sum(position)': '0 USD' },
      { account: 'Assets:Savings', 'sum(position)': '0 USD' },
    ],
  });
  vi.mocked(getTransactions).mockResolvedValue({
    status: 'ok',
    transactions: [],
    total: 0,
    page: 1,
    per_page: 100,
  });
});
afterEach(cleanup);

describe('transaction selection history', () => {
  it.each([2025, 0])(
    'carries the chosen account and year %s from the accounts page into the reader',
    async (year) => {
      __history.reset('/istota/money/accounts/');
      selectedYear.set(year);
      render(AccountsPage);
      await fireEvent.click(await screen.findByRole('button', { name: 'Checking', exact: true }));
      const target = vi.mocked(goto).mock.calls.at(-1)?.[0];
      expect(target).toBe(
        `/istota/money/transactions/?account=Assets%3AChecking&year=${year || 'all'}`,
      );
      cleanup();
      __history.reset(String(target));
      selectedAccount.set('');
      selectedYear.set(2026);
      mount();
      await waitFor(() =>
        expect(getTransactions).toHaveBeenCalledWith(
          expect.objectContaining({ account: 'Assets:Checking', year: year || undefined }),
        ),
      );
    },
  );

  it('keeps the current year default for a bare URL', async () => {
    mount();
    await waitFor(() =>
      expect(currentUrl()).toBe(`/istota/money/transactions/?year=${new Date().getFullYear()}`),
    );
    expect(get(selectedYear)).toBe(new Date().getFullYear());
    expect(__history.entries).toHaveLength(1);
  });

  it('pushes account clicks and restores the reader filter on Back and Forward', async () => {
    mount();
    await fireEvent.click(await screen.findByRole('button', { name: 'Checking' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Savings' }));
    expect(__history.entries).toHaveLength(3);
    expect(new URL(currentUrl(), location.origin).searchParams.get('account')).toBe(
      'Assets:Savings',
    );
    vi.mocked(getTransactions).mockClear();
    __history.back();
    await waitFor(() =>
      expect(getTransactions).toHaveBeenCalledWith(
        expect.objectContaining({ account: 'Assets:Checking' }),
      ),
    );
    __history.forward();
    await waitFor(() => expect(get(selectedAccount)).toBe('Assets:Savings'));
    expect(__history.entries).toHaveLength(3);
  });

  it.each(['transaction', 'posting'])(
    'pushes a %s account and restores its previous account on Back',
    async (control) => {
      const transaction = {
        date: '2026-08-20',
        flag: '*',
        payee: 'Example merchant',
        narration: 'Example purchase',
        account: 'Expenses:Supplies',
        position: '20 USD',
      };
      vi.mocked(getTransactions).mockResolvedValue({
        status: 'ok',
        transactions: [transaction],
        total: 1,
        page: 1,
        per_page: 100,
      });
      vi.mocked(getPostings).mockResolvedValue({
        status: 'ok',
        postings: [{ account: 'Assets:Wallet', position: '-20 USD' }],
      });
      mount();
      await fireEvent.click(await screen.findByRole('button', { name: 'Checking' }));
      if (control === 'posting') {
        await fireEvent.click(
          await screen.findByRole('button', { name: /Example merchant Example purchase/ }),
        );
        await fireEvent.click(
          await screen.findByRole('button', { name: 'Assets:Wallet', exact: true }),
        );
      } else {
        await fireEvent.click(
          await screen.findByRole('button', { name: 'Expenses:Supplies', exact: true }),
        );
      }
      const target = control === 'posting' ? 'Assets:Wallet' : 'Expenses:Supplies';
      await waitFor(() => expect(get(selectedAccount)).toBe(target));
      expect(__history.entries).toHaveLength(3);
      vi.mocked(getTransactions).mockClear();
      __history.back();
      await waitFor(() =>
        expect(getTransactions).toHaveBeenCalledWith(
          expect.objectContaining({ account: 'Assets:Checking' }),
        ),
      );
      expect(get(selectedAccount)).toBe('Assets:Checking');
    },
  );

  it('replaces a year change and restores All years after a reload', async () => {
    mount();
    await screen.findByRole('button', { name: 'Checking' });
    const trigger = screen.getByRole('button', { name: 'Year' });
    await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(trigger);
    const option = await screen.findByRole('option', { name: '2025' });
    await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
    await fireEvent.click(option);
    await tick();
    expect(currentUrl()).toBe('/istota/money/transactions/?year=2025');
    expect(__history.entries).toHaveLength(1);
    selectedYear.set(0);
    await tick();
    expect(currentUrl()).toBe('/istota/money/transactions/?year=all');
    cleanup();
    selectedYear.set(2026);
    __history.reset(currentUrl());
    mount();
    await waitFor(() => expect(get(selectedYear)).toBe(0));
  });

  it('restores encoded accounts and the year from a load or real navigation', async () => {
    __history.reset('/istota/money/transactions/?account=Assets%3ABank%3AChecking&year=2024');
    mount();
    await waitFor(() =>
      expect(getTransactions).toHaveBeenCalledWith(
        expect.objectContaining({ account: 'Assets:Bank:Checking', year: 2024 }),
      ),
    );
    page.state = {};
    page.url = new URL(
      '/istota/money/transactions/?account=Assets%3ASavings&year=2023',
      location.origin,
    );
    window.history.replaceState(null, '', page.url);
    await waitFor(() => expect(get(selectedYear)).toBe(2023));
    expect(get(selectedAccount)).toBe('Assets:Savings');
  });

  it('shows a valid linked year outside the recent-year menu', async () => {
    __history.reset('/istota/money/transactions/?year=1900');
    mount();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Year' }).textContent).toContain('1900'),
    );
    expect(get(selectedYear)).toBe(1900);
  });

  it('drops invalid years without losing a valid account', async () => {
    __history.reset('/istota/money/transactions/?account=Assets%3AChecking&year=2200');
    mount();
    await waitFor(() => expect(get(selectedAccount)).toBe('Assets:Checking'));
    expect(get(selectedYear)).toBe(new Date().getFullYear());
    expect(currentUrl()).toBe(
      `/istota/money/transactions/?account=Assets%3AChecking&year=${new Date().getFullYear()}`,
    );
  });
});
