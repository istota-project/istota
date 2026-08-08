import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import ElevationProfile from './ElevationProfile.svelte';
import { elevationSummary } from '$lib/location-elevation';
import type { LocationPing } from '$lib/api';

afterEach(cleanup);

function ping(timestamp: string, altitude: number | null): LocationPing {
  return {
    timestamp,
    lat: 33.9,
    lon: -118.4,
    altitude,
    accuracy: 10,
    place: null,
    speed: null,
    battery: null,
    activity_type: 'driving',
  };
}

const CLIMB = [
  ping('2026-07-30T04:14:00Z', 335.3),
  ping('2026-07-30T04:14:11Z', 512.1),
  ping('2026-07-30T04:14:22Z', 741.0),
  ping('2026-07-30T04:14:33Z', 1066.8),
  ping('2026-07-30T04:14:44Z', 1432.6),
];

const FLAT = [0, 1, 2, 3].map((i) => ping(`2026-07-30T0${i}:00:00Z`, 60 + i));

describe('ElevationProfile', () => {
  it('draws the strip and its readout for a real climb', () => {
    const { container } = render(ElevationProfile, { summary: elevationSummary(CLIMB) });

    expect(container.querySelector('svg')).not.toBeNull();
    expect(container.textContent).toContain('335 m – 1433 m');
  });

  it('renders nothing when the day is GPS noise', () => {
    // The absence is the signal, so a page may mount it unconditionally.
    const { container } = render(ElevationProfile, { summary: elevationSummary(FLAT) });

    expect(container.querySelector('svg')).toBeNull();
    expect(container.textContent?.trim()).toBe('');
  });

  it('honours the summary it is handed rather than re-deriving one', () => {
    // The page owns the verdict now — it needs the same one for the disclosure
    // toggle and the stats-bar figure, and a second scan of a 50,000-ping
    // range is not free. A component that quietly recomputed from points would
    // draw here and put the two surfaces out of step.
    const withheld = { ...elevationSummary(CLIMB), show: false, range: null };
    const { container } = render(ElevationProfile, { summary: withheld });

    expect(container.querySelector('svg')).toBeNull();
  });
});
