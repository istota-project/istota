import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import PortfolioClassificationsCard from './PortfolioClassificationsCard.svelte';
import {
  autoClassifyPortfolio,
  getPortfolioClassifications,
  putPortfolioClassification,
  deletePortfolioClassification,
} from '$lib/money/api';
import type { PortfolioClassification } from '$lib/money/api';

vi.mock('$lib/money/api', () => ({
  autoClassifyPortfolio: vi.fn(),
  getPortfolioClassifications: vi.fn(),
  putPortfolioClassification: vi.fn(),
  deletePortfolioClassification: vi.fn(),
}));

// The real notices store schedules auto-dismiss timers that outlive the test
// environment and surface as post-teardown errors in whichever file runs next.
vi.mock('$lib/stores/notices', () => ({
  notifyError: vi.fn(),
  notifyInfo: vi.fn(),
  notifySuccess: vi.fn(),
}));

const mockedGet = vi.mocked(getPortfolioClassifications);
const mockedPut = vi.mocked(putPortfolioClassification);
const mockedAuto = vi.mocked(autoClassifyPortfolio);
vi.mocked(deletePortfolioClassification);

function cls(overrides: Partial<PortfolioClassification> = {}): PortfolioClassification {
  return {
    symbol: 'VTI',
    asset_class: 'Stocks',
    sub_class: 'Total Market',
    geography: 'US',
    source: 'seed',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

// bits-ui overlays hold a body scroll lock whose reset runs after unmount;
// close anything open before cleanup so it cannot land after jsdom teardown.
afterEach(async () => {
  await fireEvent.keyDown(document.body, { key: 'Escape' });
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 0));
});

async function mount(classifications: PortfolioClassification[]) {
  mockedGet.mockResolvedValue({ status: 'ok', classifications });
  const utils = render(PortfolioClassificationsCard);
  if (classifications.length > 0) {
    await screen.findByText(classifications[0].symbol);
  } else {
    await screen.findByText('No classifications yet.');
  }
  return utils;
}

describe('PortfolioClassificationsCard', () => {
  it('renders one compact line per classification, not a table', async () => {
    await mount([
      cls(),
      cls({ symbol: 'SGOV', asset_class: 'Fixed Income', sub_class: 'Short-Term' }),
    ]);
    expect(document.querySelector('table')).toBeNull();
    const row = screen.getByText('VTI').closest('li');
    expect(row?.textContent).toContain('Stocks · Total Market · US');
  });

  it('offers dropdowns rather than free text for class, sub-class and geography', async () => {
    await mount([]);
    expect(screen.getByRole('button', { name: 'Asset class' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sub-class' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Geography' })).toBeTruthy();
    // the symbol stays the one typed field
    expect(screen.getByLabelText('Symbol')).toBeTruthy();
    expect(screen.queryByLabelText('Asset class', { selector: 'input' })).toBeNull();
  });

  it('adds a classification from the picked values', async () => {
    await mount([]);
    mockedPut.mockResolvedValue({ status: 'ok', classification: cls() });

    await fireEvent.input(screen.getByLabelText('Symbol'), { target: { value: ' vti ' } });
    const trigger = screen.getByRole('button', { name: 'Asset class' });
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    // bits-ui select items commit on pointerup, not click, and jsdom
    // synthesizes neither from a click() call.
    const option = await screen.findByRole('option', { name: 'Stocks' });
    await fireEvent.pointerDown(option);
    await fireEvent.pointerUp(option);
    await fireEvent.click(option);

    const add = screen.getByRole('button', { name: 'Add' });
    await fireEvent.click(add);
    expect(mockedPut).toHaveBeenCalledWith('vti', {
      asset_class: 'Stocks',
      sub_class: '',
      geography: '',
    });
  });

  it('opens an inline edit from the kebab, prefilled, with its own Save and Cancel', async () => {
    await mount([cls()]);
    const trigger = screen.getByLabelText('Actions for VTI');
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    await fireEvent.click(await screen.findByText('Edit'));

    // Prefilled pickers: the edit row's triggers show the stored values.
    const classTriggers = screen.getAllByRole('button', { name: 'Asset class' });
    expect(classTriggers.some((t) => t.textContent?.includes('Stocks'))).toBe(true);
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
  });

  it('badges auto-classified rows but not seeded or user rows', async () => {
    await mount([
      cls({ symbol: 'VTI', source: 'seed' }),
      cls({ symbol: 'ZZZQ', source: 'auto' }),
      cls({ symbol: 'GOOG', source: 'user' }),
    ]);
    // the card hint also says "auto" in prose, so scope to the badge
    expect(screen.getAllByText('auto', { selector: '.badge' })).toHaveLength(1);
  });

  it('runs the auto-classify card action and reloads the list', async () => {
    await mount([cls()]);
    mockedAuto.mockResolvedValue({
      status: 'ok',
      classified: [
        {
          symbol: 'ZZZQ',
          asset_class: 'Stocks',
          sub_class: 'Technology',
          geography: 'US',
          method: 'lookup',
        },
      ],
      unresolved: [],
    });
    mockedGet.mockClear();
    await fireEvent.click(screen.getByRole('button', { name: 'Auto-classify' }));
    expect(mockedAuto).toHaveBeenCalledOnce();
    // The list reloads so the new row and its badge appear.
    expect(mockedGet).toHaveBeenCalled();
  });
});
