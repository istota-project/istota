import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import PortfolioAccountsCard from './PortfolioAccountsCard.svelte';
import { settingsSave } from '$lib/stores/settingsSave.svelte';
import { getPortfolioAccounts, patchPortfolioAccount } from '$lib/money/api';
import type { PortfolioAccount } from '$lib/money/api';

vi.mock('$lib/money/api', () => ({
  getPortfolioAccounts: vi.fn(),
  patchPortfolioAccount: vi.fn(),
}));

// The real notices store schedules auto-dismiss timers that outlive the test
// environment and surface as post-teardown errors in whichever file runs next.
vi.mock('$lib/stores/notices', () => ({
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
}));

const mockedGet = vi.mocked(getPortfolioAccounts);
const mockedPatch = vi.mocked(patchPortfolioAccount);

function account(overrides: Partial<PortfolioAccount> = {}): PortfolioAccount {
  return {
    id: 1,
    account_name: 'Taxable Brokerage',
    account_number: 'X111',
    group: 'Alice',
    account_type: 'taxable',
    excluded: false,
    first_seen_at: '2026-01-01T00:00:00Z',
    last_seen_at: '2026-07-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(cleanup);

async function mount(accounts: PortfolioAccount[]) {
  mockedGet.mockResolvedValue({ status: 'ok', accounts });
  const utils = render(PortfolioAccountsCard);
  if (accounts.length > 0) {
    await screen.findByText(accounts[0].account_name);
  } else {
    await screen.findByText(/No portfolio accounts yet/);
  }
  return utils;
}

describe('PortfolioAccountsCard', () => {
  it('renders an editable row per account with no per-row Save button', async () => {
    await mount([
      account(),
      account({ id: 2, account_name: 'Roth IRA A', account_number: 'X222' }),
    ]);
    expect(screen.getByLabelText('Group of Taxable Brokerage')).toBeTruthy();
    expect(screen.getByLabelText('Type of Roth IRA A')).toBeTruthy();
    // Saving is the app bar's job now — no button per row.
    expect(screen.queryByRole('button', { name: /save/i })).toBeNull();
  });

  it('registers with the app-bar save and patches only the dirty rows', async () => {
    await mount([account(), account({ id: 2, account_name: 'Roth IRA A' })]);
    expect(get(settingsSave)?.dirty).toBe(false);

    const group = screen.getByLabelText('Group of Taxable Brokerage');
    await fireEvent.input(group, { target: { value: 'Maria' } });
    await tick();
    const agg = get(settingsSave);
    expect(agg?.dirty).toBe(true);

    mockedPatch.mockResolvedValue({ status: 'ok', account: account({ group: 'Maria' }) });
    await agg!.save();
    expect(mockedPatch).toHaveBeenCalledTimes(1);
    expect(mockedPatch).toHaveBeenCalledWith(1, {
      group: 'Maria',
      account_type: 'taxable',
      excluded: false,
    });
    await tick();
    expect(get(settingsSave)?.dirty).toBe(false);
  });

  it('offers existing groups as autocomplete suggestions on the group field', async () => {
    const { container } = await mount([
      account({ group: 'Alice' }),
      account({ id: 2, account_name: 'Joint Brokerage', group: 'Bob' }),
      account({ id: 3, account_name: 'Joint Cash', group: '' }),
    ]);
    const input = screen.getByLabelText('Group of Taxable Brokerage') as HTMLInputElement;
    expect(input.getAttribute('list')).toBe('portfolio-account-groups');
    const options = Array.from(container.querySelectorAll('#portfolio-account-groups option')).map(
      (o) => o.getAttribute('value'),
    );
    // Existing labels only, deduped, no entry for the ungrouped row.
    expect(options).toEqual(['Bob', 'Alice']);
  });

  it('marks the page dirty when the excluded flag is toggled', async () => {
    await mount([account()]);
    const checkbox = screen.getByLabelText('Exclude Taxable Brokerage from summaries');
    await fireEvent.click(checkbox);
    await tick();
    expect(get(settingsSave)?.dirty).toBe(true);
  });

  it('keeps a row dirty when its patch fails, so Save can retry it', async () => {
    await mount([account()]);
    const group = screen.getByLabelText('Group of Taxable Brokerage');
    await fireEvent.input(group, { target: { value: 'Maria' } });
    await tick();

    mockedPatch.mockRejectedValue(new Error('nope'));
    await get(settingsSave)!.save();
    await tick();
    expect(get(settingsSave)?.dirty).toBe(true);
  });

  it('withdraws from the app-bar save when there are no accounts', async () => {
    await mount([]);
    await tick();
    expect(get(settingsSave)).toBeNull();
  });
});
