import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/svelte';
import { __history } from '../../../../vitest-stubs/app-navigation';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

// Coarse, at the module boundary: jsdom has no 2d context, and what is under
// test is only whether the page lets go of the instance it made.
const chartJs = vi.hoisted(() => ({ created: 0, destroyed: 0 }));
vi.mock('chart.js', () => ({
  Chart: class {
    static register() {}
    constructor() {
      chartJs.created += 1;
    }
    destroy() {
      chartJs.destroyed += 1;
    }
  },
  LineController: class {},
  LineElement: class {},
  PointElement: class {},
  CategoryScale: class {},
  LinearScale: class {},
  Tooltip: class {},
  Filler: class {},
}));
import { getHealthSettings, healthStatsSeries, listHealthStats } from '$lib/api';
import Page from './+page.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  chartJs.created = 0;
  chartJs.destroyed = 0;
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
  vi.mocked(healthStatsSeries).mockResolvedValue({
    metric: 'weight',
    points: [{ measured_at: '2026-08-20T09:00:00Z', value: 70, unit: 'kg' }],
  });
});
afterEach(cleanup);

describe('Chart.js on unmount', () => {
  it('destroys every chart the page built when it unmounts', async () => {
    const view = render(Page);
    await waitFor(() => expect(chartJs.created).toBeGreaterThan(0));
    const live = chartJs.created - chartJs.destroyed;
    expect(live).toBeGreaterThan(0);
    view.unmount();
    expect(chartJs.created - chartJs.destroyed).toBe(0);
  });

  // The other four chart pages need a ledger or a marker to mount at all, so
  // they are held by their source: a page that builds a Chart tears it down.
  it('gives every file that builds a Chart an onDestroy', () => {
    const SRC = resolve(__dirname, '../../..');
    const walk = (dir: string): string[] =>
      readdirSync(dir).flatMap((name) => {
        const full = join(dir, name);
        return statSync(full).isDirectory() ? walk(full) : full.endsWith('.svelte') ? [full] : [];
      });
    const builders = walk(SRC).filter((f) => /new Chart\(/.test(readFileSync(f, 'utf8')));
    expect(builders.length).toBeGreaterThanOrEqual(5);
    const missing = builders
      .filter((f) => !/onDestroy\(/.test(readFileSync(f, 'utf8')))
      .map((f) => relative(SRC, f));
    expect(missing).toEqual([]);
  });
});
