/**
 * The "Log measurement" modal on Health → Stats (ISSUE-358).
 *
 * Named without the `+` for the reason `dashboard.svelte.test.ts` is:
 * SvelteKit reserves that prefix in `src/routes/` and `svelte-kit sync`
 * refuses to build a manifest when it finds a name it does not recognize.
 *
 * ---
 *
 * `submitEntry` guarded on `formValue.trim()`. `formValue` is declared as a
 * string and bound to `<Input type="number">`, and Svelte's `bind_value`
 * coerces on every input event when the element is number-like — a runtime
 * check on `input.type`, so it applies even though `Input.svelte` passes the
 * type through as a prop rather than writing it as a literal. The moment
 * anyone typed, `formValue` held a number, `number.trim` did not exist, and
 * the throw escaped the handler entirely: the guard sits outside the `try`,
 * so no error banner was drawn and the button never entered its loading
 * state. The console got a TypeError and the modal sat there.
 *
 * The one case that worked was the one with nothing to save.
 *
 * There is one modal and one submit handler behind all of body stats, so this
 * was every metric rather than only the weight the report came from — which
 * is what the resting-HR case here holds.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import type { HealthSettings, HealthStat } from '$lib/api';

vi.mock('$lib/api', () => ({
  createHealthStat: vi.fn(),
  deleteHealthStat: vi.fn(),
  getHealthSettings: vi.fn(),
  healthStatsSeries: vi.fn(),
  listHealthStats: vi.fn(),
}));

// The page draws a sparkline per metric card. Chart.js wants a real 2d
// context and jsdom has none, and the chart is not what is under test.
vi.mock('chart.js', () => {
  class Chart {
    static register() {}
    destroy() {}
  }
  return {
    Chart,
    LineController: class {},
    LineElement: class {},
    PointElement: class {},
    CategoryScale: class {},
    LinearScale: class {},
    Tooltip: class {},
    Filler: class {},
  };
});

import { createHealthStat, getHealthSettings, healthStatsSeries, listHealthStats } from '$lib/api';
import Page from './+page.svelte';

const SETTINGS: HealthSettings = {
  dob: null,
  height_cm: null,
  sex: null,
  display_units: { weight: 'kg', height: 'cm', temp: 'C' },
};

function stat(metric: string, value: number, unit: string): HealthStat {
  return {
    id: 1,
    measured_at: '2026-08-20T09:00:00Z',
    metric,
    value,
    unit,
    source: 'manual',
    notes: null,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getHealthSettings).mockResolvedValue({ settings: SETTINGS });
  vi.mocked(listHealthStats).mockResolvedValue({ stats: [] });
  vi.mocked(healthStatsSeries).mockResolvedValue({ metric: '', points: [] });
  vi.mocked(createHealthStat).mockResolvedValue({ status: 'ok', id: 7 });
});

afterEach(cleanup);

/** Mount the page and wait out its onMount load. */
async function mountPage() {
  render(Page);
  await screen.findByRole('button', { name: '+ Log measurement' });
}

async function openModal(opener: string | RegExp) {
  await fireEvent.click(screen.getByRole('button', { name: opener }));
  return await screen.findByRole('dialog');
}

/**
 * Type into the value field the way a browser does — set the value, then
 * dispatch `input`, which is the event `bind_value` listens to and the point
 * at which the coercion happens.
 */
async function typeValue(dialog: HTMLElement, text: string) {
  const input = screen.getByLabelText('Value') as HTMLInputElement;
  expect(input.type).toBe('number');
  await fireEvent.input(input, { target: { value: text } });
  return input;
}

async function save(dialog: HTMLElement) {
  const form = dialog.querySelector('form');
  if (!form) throw new Error('the modal rendered no form');
  await fireEvent.submit(form);
}

describe('logging a measurement', () => {
  it('saves the value that was typed', async () => {
    await mountPage();
    const dialog = await openModal('+ Log measurement');
    await typeValue(dialog, '72.4');
    await save(dialog);

    expect(createHealthStat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(createHealthStat).mock.calls[0][0]).toMatchObject({
      metric: 'weight',
      value: 72.4,
      unit: 'kg',
    });
  });

  it('saves a metric that is not weight', async () => {
    // One modal and one handler serve all of body stats, so the defect was
    // never weight-specific. Opening from a metric card is also the path the
    // report did not take, and it reaches the same submit.
    vi.mocked(listHealthStats).mockResolvedValue({ stats: [stat('resting_hr', 58, 'bpm')] });
    vi.mocked(healthStatsSeries).mockResolvedValue({
      metric: 'resting_hr',
      points: [{ measured_at: '2026-08-20T09:00:00Z', value: 58, unit: 'bpm' }],
    });

    await mountPage();
    const dialog = await openModal(/^Resting HR/);
    await typeValue(dialog, '61');
    await save(dialog);

    expect(createHealthStat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(createHealthStat).mock.calls[0][0]).toMatchObject({
      metric: 'resting_hr',
      value: 61,
      unit: 'bpm',
    });
  });

  it('converts to the canonical unit before sending', async () => {
    // The value has to survive as a number all the way into `toCanonical`,
    // which is the other half of what the string guard broke.
    await mountPage();
    const dialog = await openModal('+ Log measurement');
    await fireEvent.change(screen.getByLabelText('Unit'), { target: { value: 'lb' } });
    await typeValue(dialog, '160');
    await save(dialog);

    const body = vi.mocked(createHealthStat).mock.calls[0][0];
    expect(body.unit).toBe('kg');
    expect(body.value).toBeCloseTo(72.57, 1);
  });

  it('sends nothing when the field is empty, and still works afterwards', async () => {
    // The branch the guard was written for. Clearing a number input hands the
    // binding `null` rather than the empty string it was declared with, so
    // this is the same defect wearing a different value.
    //
    // The second half is what makes the test able to fail. "Nothing was sent"
    // passes against the broken code too — the throw also sends nothing — so
    // on its own it proves an early return that never happened. Saving after
    // the empty submit separates a guard that returned from a handler that
    // died.
    await mountPage();
    const dialog = await openModal('+ Log measurement');
    const input = await typeValue(dialog, '72.4');
    await fireEvent.input(input, { target: { value: '' } });
    await save(dialog);

    expect(createHealthStat).not.toHaveBeenCalled();

    await fireEvent.input(input, { target: { value: '73' } });
    await save(dialog);

    expect(createHealthStat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(createHealthStat).mock.calls[0][0]).toMatchObject({ value: 73 });
  });

  it('reports a failed save in the modal instead of leaving it silent', async () => {
    // The guard sat outside the `try`, so the original failure had no surface
    // at all. Whatever else changes, a rejection has to reach the banner.
    vi.mocked(createHealthStat).mockRejectedValue(new Error('Panel is locked'));

    await mountPage();
    const dialog = await openModal('+ Log measurement');
    await typeValue(dialog, '72.4');
    await save(dialog);

    expect(await screen.findByText('Panel is locked')).toBeTruthy();
  });
});
