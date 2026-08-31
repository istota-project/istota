import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DaySummary, LocationPing } from '$lib/api';

vi.mock('$lib/api', () => ({
  getLocationCurrent: vi.fn(),
  getLocationPings: vi.fn(),
  getDaySummary: vi.fn(),
}));

vi.mock('$lib/components/location/LocationMap.svelte', () => ({
  default: () => ({ flyTo: vi.fn() }),
}));

import { getDaySummary, getLocationCurrent, getLocationPings } from '$lib/api';
import Page from './+page.svelte';

const PING: LocationPing = {
  timestamp: '2026-09-01T00:00:00Z',
  lat: 34,
  lon: -118,
  altitude: null,
  accuracy: 5,
  place: 'Home',
  speed: 0,
  battery: 0.8,
  activity_type: 'stationary',
};

function summary(date: string, stops = 0): DaySummary {
  return {
    date,
    timezone: 'America/Los_Angeles',
    ping_count: stops,
    transit_pings: stops ? 2 : 0,
    stops: stops
      ? [
          {
            location: 'Home',
            location_source: 'place',
            arrived: `${date}T00:00:00Z`,
            departed: `${date}T00:10:00Z`,
            ping_count: 1,
            lat: PING.lat,
            lon: PING.lon,
          },
        ]
      : [],
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 7, 31, 23, 59, 30));
  vi.mocked(getLocationCurrent).mockResolvedValue({ last_ping: null, current_visit: null });
  vi.mocked(getLocationPings).mockResolvedValue({ pings: [], count: 0 });
  vi.mocked(getDaySummary).mockResolvedValue(summary('2026-08-31'));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('location polling', () => {
  it('refreshes the whole view and rolls the date over at midnight', async () => {
    render(Page);

    await waitFor(() => expect(getLocationCurrent).toHaveBeenCalledTimes(1));
    expect(getLocationPings).toHaveBeenLastCalledWith({ date: '2026-08-31' });
    expect(getDaySummary).toHaveBeenLastCalledWith('2026-08-31');

    vi.mocked(getLocationPings).mockResolvedValueOnce({ pings: [PING], count: 1 });
    vi.mocked(getDaySummary).mockResolvedValueOnce(summary('2026-09-01', 1));
    await vi.advanceTimersByTimeAsync(60_000);

    expect(getLocationCurrent).toHaveBeenCalledTimes(2);
    expect(getLocationPings).toHaveBeenCalledTimes(2);
    expect(getLocationPings).toHaveBeenLastCalledWith({ date: '2026-09-01' });
    expect(getDaySummary).toHaveBeenCalledTimes(2);
    expect(getDaySummary).toHaveBeenLastCalledWith('2026-09-01');
    expect(screen.getByText('1 pings')).toBeInTheDocument();
    expect(screen.getByText('1 stops')).toBeInTheDocument();
    expect(screen.getByText('2 transit')).toBeInTheDocument();
  });

  it('applies successful poll results when one endpoint fails', async () => {
    render(Page);
    await waitFor(() => expect(getLocationCurrent).toHaveBeenCalledTimes(1));

    vi.mocked(getLocationCurrent).mockResolvedValueOnce({ last_ping: PING, current_visit: null });
    vi.mocked(getLocationPings).mockResolvedValueOnce({ pings: [PING], count: 1 });
    vi.mocked(getDaySummary).mockRejectedValueOnce(new Error('summary unavailable'));
    await vi.advanceTimersByTimeAsync(60_000);

    expect(screen.getByText('Home')).toBeInTheDocument();
    expect(screen.getByText('1 pings')).toBeInTheDocument();
    expect(screen.queryByText('Failed to load location data')).not.toBeInTheDocument();
  });

  it('does not start another poll while one is still running', async () => {
    const pending = new Promise<never>(() => {});
    vi.mocked(getLocationCurrent).mockReturnValue(pending);
    vi.mocked(getLocationPings).mockReturnValue(pending);
    vi.mocked(getDaySummary).mockReturnValue(pending);

    render(Page);
    expect(getLocationCurrent).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(60_000);

    expect(getLocationCurrent).toHaveBeenCalledTimes(1);
    expect(getLocationPings).toHaveBeenCalledTimes(1);
    expect(getDaySummary).toHaveBeenCalledTimes(1);
  });
});
