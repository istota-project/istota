import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { Medication } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

const notices = vi.hoisted(() => ({
  notify: vi.fn(),
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('@minot/ui/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...notices,
}));

import Page from './+page.svelte';

function med(overrides: Partial<Medication> & { id: number; name: string }): Medication {
  return {
    generic_name: null,
    dose: null,
    dose_unit: null,
    frequency: null,
    route: null,
    category: 'prescription',
    prescriber: null,
    pharmacy: null,
    reason: null,
    start_date: null,
    end_date: null,
    active: true,
    encounter_id: null,
    notes: null,
    source: 'manual',
    created_at: '2026-01-01 00:00:00',
    document_count: 0,
    ...overrides,
  };
}

const statin = med({
  id: 1,
  name: 'Lipitor',
  generic_name: 'Atorvastatin',
  dose: '20',
  dose_unit: 'mg',
  frequency: 'daily',
  prescriber: 'Dr. Example',
  start_date: '2026-01-15',
});
const vitamin = med({ id: 2, name: 'Vitamin D3', category: 'supplement', dose: '5000' });
const painkiller = med({ id: 3, name: 'Ibuprofen', category: 'otc' });
const oldCourse = med({
  id: 4,
  name: 'Amoxicillin',
  active: false,
  start_date: '2025-03-01',
  end_date: '2025-03-10',
});
const laterCourse = med({
  id: 5,
  name: 'Prednisone',
  active: false,
  start_date: '2025-06-01',
  end_date: '2025-06-08',
});
const unknownStop = med({ id: 6, name: 'Melatonin', category: 'supplement', active: false });

function tableRows(label: string): string[] {
  const table = screen.getByRole('table', { name: label });
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => row.querySelector('td.name .title-card')?.textContent ?? '');
}

async function choose(item: string, name: string) {
  await fireEvent.click(screen.getByRole('button', { name: `Actions for ${name}` }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: item }));
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listMedications.mockResolvedValue({
    medications: [statin, vitamin, painkiller, oldCourse, unknownStop, laterCourse],
  });
});

afterEach(() => cleanup());

describe('medications list', () => {
  it('loads every row once and splits it into the three sections', async () => {
    render(Page);
    await screen.findByRole('table', { name: 'Prescriptions' });
    expect(api.listMedications).toHaveBeenCalledTimes(1);
    expect(api.listMedications).toHaveBeenCalledWith({ status: 'all', limit: 500 });

    expect(tableRows('Prescriptions')).toEqual(['Lipitor']);
    expect(tableRows('OTC and supplements')).toEqual(['Vitamin D3', 'Ibuprofen']);
    // Newest end date first, an unknown end date last.
    expect(tableRows('Stopped')).toEqual(['Prednisone', 'Amoxicillin', 'Melatonin']);

    const prescriptions = screen.getByRole('table', { name: 'Prescriptions' });
    expect(within(prescriptions).getByText('Atorvastatin')).toBeTruthy();
    expect(within(prescriptions).getByText('Dr. Example')).toBeTruthy();
    expect(within(prescriptions).getByText('Rx')).toBeTruthy();
    const other = screen.getByRole('table', { name: 'OTC and supplements' });
    expect(within(other).queryByRole('columnheader', { name: 'Prescriber' })).toBeNull();
    expect(within(other).getByText('Supplement')).toBeTruthy();
    expect(within(other).getByText('OTC')).toBeTruthy();
  });

  it('shows one empty state when nothing is current', async () => {
    api.listMedications.mockResolvedValue({ medications: [] });
    render(Page);
    expect(await screen.findByText('No current medications recorded.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText('Prescriptions')).toBeNull();
  });

  it('gives an empty section its own empty state', async () => {
    api.listMedications.mockResolvedValue({ medications: [vitamin] });
    render(Page);
    expect(await screen.findByText('No current prescriptions.')).toBeTruthy();
    expect(tableRows('OTC and supplements')).toEqual(['Vitamin D3']);
  });

  it('stops a medication on the date the dialog holds', async () => {
    api.discontinueMedication.mockResolvedValue({ status: 'ok', end_date: '2026-04-02' });
    render(Page);
    await screen.findByRole('table', { name: 'Prescriptions' });
    await choose('Stop', 'Lipitor');

    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByLabelText('End date');
    await fireEvent.input(input, { target: { value: '2026-04-02' } });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Stop' }));

    await waitFor(() => expect(api.discontinueMedication).toHaveBeenCalledWith(1, '2026-04-02'));
    await waitFor(() => expect(api.listMedications).toHaveBeenCalledTimes(2));
  });

  it('offers no Stop on a stopped row', async () => {
    render(Page);
    await screen.findByRole('table', { name: 'Stopped' });
    await fireEvent.click(screen.getByRole('button', { name: 'Actions for Amoxicillin' }));
    await screen.findByRole('menuitem', { name: 'Delete' });
    expect(screen.queryByRole('menuitem', { name: 'Stop' })).toBeNull();
  });

  it('reports a failed delete out of band and keeps the row', async () => {
    api.deleteMedication.mockRejectedValue(new Error('medication not found'));
    render(Page);
    await screen.findByRole('table', { name: 'Prescriptions' });
    await choose('Delete', 'Lipitor');
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('medication not found'));
    expect(api.deleteMedication).toHaveBeenCalledWith(1);
    expect(tableRows('Prescriptions')).toEqual(['Lipitor']);
    expect(api.listMedications).toHaveBeenCalledTimes(1);
  });

  it('saves a new medication from the add form and reloads', async () => {
    api.createMedication.mockResolvedValue({ status: 'ok', id: 9 });
    api.listMedications.mockResolvedValue({ medications: [] });
    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: '+ Add' }));
    await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'Fish oil' } });
    await fireEvent.input(screen.getByLabelText('Start date'), {
      target: { value: '2026-02-01' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.createMedication).toHaveBeenCalledTimes(1));
    expect(api.createMedication.mock.calls[0][0]).toMatchObject({
      name: 'Fish oil',
      category: 'prescription',
      start_date: '2026-02-01',
    });
  });
});
