/**
 * Health settings' unsaved-changes check.
 *
 * Named without the `+` for the reason `dashboard.svelte.test.ts` is: SvelteKit
 * reserves that prefix in `src/routes/`.
 *
 * The check compares the visible height input only. A unit toggle fills in the
 * other input from the one on screen, so toggling back must not leave the form
 * holding an edit nobody made.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { HealthSettings } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import { getHealthSettings, putHealthSettings } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';
import Page from './+page.svelte';

const SETTINGS: HealthSettings = {
  dob: '1980-04-02',
  height_cm: 178,
  sex: null,
  display_units: { weight: 'kg', height: 'cm', temp: 'C' },
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getHealthSettings).mockResolvedValue({ settings: structuredClone(SETTINGS) });
  vi.mocked(putHealthSettings).mockImplementation(async (body) => ({
    status: 'ok',
    settings: { ...structuredClone(SETTINGS), ...body } as HealthSettings,
  }));
});
afterEach(cleanup);

const dirty = () => get(settingsSave)?.dirty;

async function mountPage() {
  render(Page);
  await screen.findByRole('heading', { name: 'Display preferences' });
}

const heightCm = () => screen.getByPlaceholderText('178') as HTMLInputElement;

async function pickHeightUnit(label: string) {
  const card = screen.getByRole('heading', { name: 'Display preferences' }).closest('section')!;
  const trigger = within(card).getByRole('button', { name: 'Height unit' });
  const mouse = { pointerType: 'mouse', button: 0 };
  await fireEvent.pointerDown(trigger, mouse);
  await fireEvent.pointerUp(trigger, mouse);
  const option = await screen.findByRole('option', { name: label });
  await fireEvent.pointerMove(option, { pointerType: 'mouse' });
  await fireEvent.pointerDown(option, mouse);
  await fireEvent.pointerUp(option, mouse);
}

describe('health settings unsaved changes', () => {
  it('turns dirty on an edit and clean again once it is saved', async () => {
    await mountPage();
    expect(dirty()).toBe(false);

    await fireEvent.input(heightCm(), { target: { value: '180' } });
    await waitFor(() => expect(dirty()).toBe(true));

    await get(settingsSave)!.save();
    expect(vi.mocked(putHealthSettings).mock.calls[0][0].height_cm).toBe(180);
    await waitFor(() => expect(dirty()).toBe(false));
  });

  it('holds no edit after the height unit is switched and switched back', async () => {
    await mountPage();
    await pickHeightUnit('ft / in');
    await waitFor(() => expect(dirty()).toBe(true));
    expect(screen.getByRole('spinbutton', { name: 'Height in feet' })).toHaveValue(5);

    await pickHeightUnit('cm');
    await waitFor(() => expect(dirty()).toBe(false));
    expect(heightCm()).toHaveValue(178);
  });
});
