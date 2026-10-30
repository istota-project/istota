import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { TaxEstimateResponse } from '$lib/money/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/money/api', async (original) => ({
  ...(await original<typeof import('$lib/money/api')>()),
  getTaxEstimate: vi.fn(),
  recalculateTaxEstimate: vi.fn(),
}));
import { getTaxEstimate, recalculateTaxEstimate } from '$lib/money/api';
import Page from './+page.svelte';

const ZERO_FIELDS = [
  'se_income_ytd',
  'se_income_annualized',
  'w2_income',
  'w2_income_annualized',
  'se_tax',
  'half_se_deduction',
  'additional_medicare_tax',
  'federal_agi',
  'federal_standard_deduction',
  'federal_taxable_income',
  'federal_tax',
  'qbi_deduction',
  'state_agi',
  'state_standard_deduction',
  'state_taxable_income',
  'state_personal_exemption',
  'state_tax',
  'federal_withholding',
  'state_withholding',
  'federal_estimated_paid',
  'state_estimated_paid',
  'federal_total_liability',
  'state_total_liability',
  'federal_net_due',
  'state_net_due',
  'federal_quarterly_amount',
  'state_quarterly_amount',
  'quarters_remaining',
  'ss_wage_base',
];

function estimate(): TaxEstimateResponse {
  return {
    ...Object.fromEntries(ZERO_FIELDS.map((name) => [name, 0])),
    status: 'ok',
    tax_year: 2026,
    quarter: 1,
    method: 'annualized',
    filing_status: 'single',
    w2_months: 12,
    annualization_months: 3,
    annualization_period_months: 3,
    se_income_through: '2026-03-31',
    se_income_in_progress: null,
    se_taxable_fraction: 0.9235,
    state_installment_schedule: [],
    state: '',
    state_name: '',
    state_starts_from: '',
    state_available: false,
    state_unavailable_reason: 'no_state',
    federal_rates: {
      year: 2026,
      requested_year: 2026,
      is_fallback: false,
      is_stale: false,
      overridden: false,
      source: 'fixture',
      source_url: '',
      verified_on: '',
    },
    state_rates: null,
  } as unknown as TaxEstimateResponse;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('taxes recalculation debounce', () => {
  it('does not recalculate after the page has unmounted', async () => {
    vi.mocked(getTaxEstimate).mockResolvedValue(estimate());
    vi.mocked(recalculateTaxEstimate).mockResolvedValue(estimate());
    const view = render(Page);
    const input = await screen.findByLabelText('W-2 income YTD');
    vi.useFakeTimers();
    await fireEvent.input(input, { target: { value: '1000' } });
    view.unmount();
    await vi.advanceTimersByTimeAsync(1000);
    expect(recalculateTaxEstimate).not.toHaveBeenCalled();
  });
});

describe('SE income period', () => {
  it('shows the current month beside the projection when income is posted', async () => {
    vi.mocked(getTaxEstimate).mockResolvedValue({
      ...estimate(),
      annualization_months: 9,
      se_income_through: '2026-09-30',
      se_income_in_progress: { through: '2026-10-30', amount: 450 },
    });
    render(Page);
    expect(await screen.findByText('not yet in the projection')).toBeTruthy();
    expect(screen.getByText(/October/)).toBeTruthy();
    expect(screen.getByText('$450.00')).toBeTruthy();
  });

  it('has no current month line when there is nothing to show', async () => {
    vi.mocked(getTaxEstimate).mockResolvedValue(estimate());
    render(Page);
    await screen.findByText('How income is annualized');
    expect(screen.queryByText('not yet in the projection')).toBeNull();
  });

  it.each([
    [9, '2026-09-30', 'Sep 30', true],
    [12, '2026-12-31', 'Dec 31', false],
  ])('labels the net income cutoff for %i months', async (months, through, label, capped) => {
    vi.mocked(getTaxEstimate).mockResolvedValue({
      ...estimate(),
      quarter: 4,
      annualization_months: months,
      annualization_period_months: 12,
      se_income_through: through,
    });
    render(Page);
    expect(
      await screen.findByText(
        (_, element) =>
          element?.classList.contains('breakdown-label') === true &&
          element.textContent?.replace(/\s+/g, ' ').trim() === `SE income (net, through ${label})`,
      ),
    ).toBeTruthy();
    expect(screen.queryByText('SE income (YTD)')).toBeNull();
    await fireEvent.click(screen.getByText('How income is annualized'));
    expect(screen.getByText(/For Q4, the IRS period is the first 12 months/)).toBeTruthy();
    expect(screen.getAllByText(new RegExp(`12/${months}`)).length).toBeGreaterThan(0);
    expect(!!screen.queryByText(/This projection uses a shorter span/)).toBe(capped);
  });
});
