import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent, screen } from '@testing-library/svelte';
import { __history } from '$app/navigation';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
vi.mock('chart.js', () => ({
  Chart: class {
    static register() {}
    destroy() {}
  },
  LineController: class {},
  LineElement: class {},
  PointElement: class {},
  CategoryScale: class {},
  LinearScale: class {},
  Tooltip: class {},
  Filler: class {},
}));
import { getHealthSettings, listHealthStats, healthStatsSeries } from '$lib/api';
import Page from './+page.svelte';

const currentUrl = () => __history.entries[__history.index].url;
beforeEach(() => {
  vi.clearAllMocks();
  __history.reset('/istota/health/stats/');
  vi.mocked(getHealthSettings).mockResolvedValue({
    settings: {
      dob: null,
      height_cm: null,
      sex: null,
      display_units: { weight: 'kg', height: 'cm', temp: 'C' },
    },
  });
  vi.mocked(listHealthStats).mockResolvedValue({
    stats: [
      {
        id: 1,
        metric: 'weight',
        measured_at: '2026-08-20T09:00:00Z',
        value: 70,
        unit: 'kg',
        source: 'manual',
        notes: null,
      },
    ],
  });
  vi.mocked(healthStatsSeries).mockResolvedValue({ metric: 'weight', points: [] });
});
afterEach(cleanup);

describe('health range history', () => {
  it('replaces range changes and restores the range on reload', async () => {
    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'all', exact: true }));
    await waitFor(() => expect(healthStatsSeries).toHaveBeenLastCalledWith('weight', {}));
    expect(currentUrl()).toBe('/istota/health/stats/?range=all');
    expect(__history.entries).toHaveLength(1);
    cleanup();
    vi.mocked(healthStatsSeries).mockClear();
    __history.reset(currentUrl());
    render(Page);
    await waitFor(() => expect(healthStatsSeries).toHaveBeenCalledWith('weight', {}));
    expect(
      (await screen.findByRole('button', { name: 'all', exact: true })).classList.contains(
        'active',
      ),
    ).toBe(true);
  });

  it('replaces an invalid range with the default', async () => {
    __history.reset('/istota/health/stats/?range=invalid');
    render(Page);
    await waitFor(() => expect(currentUrl()).toBe('/istota/health/stats/?range=90d'));
    expect((await screen.findByRole('button', { name: '90d' })).classList.contains('active')).toBe(
      true,
    );
    expect(__history.entries).toHaveLength(1);
  });
});
