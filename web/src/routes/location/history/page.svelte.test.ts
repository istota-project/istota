import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

vi.mock('$lib/components/location/LocationMap.svelte', () => ({
  default: () => ({}),
}));

import { getDaySummary, getLocationPings } from '$lib/api';
import { __history } from '../../../../vitest-stubs/app-navigation';
import Page from './+page.svelte';

beforeEach(() => {
  __history.reset('/istota/location/history');
  localStorage.clear();
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 0, 1, 0, 30));
  vi.mocked(getLocationPings).mockResolvedValue({ pings: [], count: 0 });
  vi.mocked(getDaySummary).mockResolvedValue({
    date: '2025-12-31',
    timezone: 'UTC',
    ping_count: 0,
    transit_pings: 0,
    stops: [],
    activities: [],
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

it('opens yesterday across a year boundary in the controls and day requests', async () => {
  render(Page);
  await waitFor(() => expect(getDaySummary).toHaveBeenCalledTimes(1));
  expect(getDaySummary).toHaveBeenCalledWith('2025-12-31');
  expect(getLocationPings).toHaveBeenCalledWith({ date: '2025-12-31' });
  expect(screen.getByLabelText('From date')).toHaveValue('2025-12-31');
  expect(screen.getByLabelText('To date')).toHaveValue('2025-12-31');
  expect(screen.getByRole('button', { name: 'Date range' })).toHaveTextContent('Yesterday');
});

it('keeps an explicit day from a link', async () => {
  __history.reset('/istota/location/history?date=2026-01-01');
  render(Page);
  await waitFor(() => expect(getDaySummary).toHaveBeenCalledWith('2026-01-01'));
  expect(getLocationPings).toHaveBeenCalledWith({ date: '2026-01-01' });
  expect(screen.getByRole('button', { name: 'Date range' })).toHaveTextContent('Today');
});

it('keeps an explicit range from a link', async () => {
  __history.reset('/istota/location/history?start=2025-12-20&end=2025-12-25');
  render(Page);
  await waitFor(() =>
    expect(getLocationPings).toHaveBeenCalledWith({
      start: '2025-12-20',
      end: '2025-12-25',
      limit: '50000',
    }),
  );
  expect(getDaySummary).not.toHaveBeenCalled();
  expect(screen.getByLabelText('From date')).toHaveValue('2025-12-20');
  expect(screen.getByLabelText('To date')).toHaveValue('2025-12-25');
});
