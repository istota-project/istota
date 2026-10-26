import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { Medication } from '$lib/api';
import { page } from '../../../../../vitest-stubs/app-state.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

const nav = vi.hoisted(() => ({ goto: vi.fn() }));
vi.mock('$app/navigation', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  goto: nav.goto,
}));

const notices = vi.hoisted(() => ({ notifyError: vi.fn() }));
vi.mock('@minot/ui/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...notices,
}));

import Page from './+page.svelte';

function med(overrides: Partial<Medication> = {}): Medication {
  return {
    id: 7,
    name: 'Lipitor',
    generic_name: 'Atorvastatin',
    dose: '20',
    dose_unit: 'mg',
    frequency: 'daily',
    route: 'oral',
    category: 'prescription',
    prescriber: 'Dr. Example',
    pharmacy: null,
    reason: 'Cholesterol',
    start_date: '2026-01-15',
    end_date: null,
    active: true,
    encounter_id: 3,
    notes: null,
    source: 'agent',
    created_at: '2026-01-15 09:00:00',
    ...overrides,
  };
}

const encounter = {
  id: 3,
  encounter_date: '2026-01-15',
  encounter_type: 'visit',
  provider: 'Dr. Example',
  facility: null,
};

function form(): HTMLFormElement {
  return screen.getByLabelText('Name').closest('form') as HTMLFormElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  page.url = new URL('http://localhost/istota/health/medications/detail?id=7');
  api.getMedication.mockResolvedValue({ medication: med(), encounter, documents: [] });
  api.updateMedication.mockResolvedValue({ status: 'ok' });
  api.deleteMedication.mockResolvedValue({ status: 'ok' });
});

afterEach(() => cleanup());

describe('medication detail', () => {
  it('loads the row into the form, with its encounter and documents', async () => {
    render(Page);
    const name = (await screen.findByLabelText('Name')) as HTMLInputElement;
    expect(api.getMedication).toHaveBeenCalledWith(7);
    expect(name.value).toBe('Lipitor');
    expect((screen.getByLabelText('Generic name') as HTMLInputElement).value).toBe('Atorvastatin');
    expect((screen.getByLabelText('Dose') as HTMLInputElement).value).toBe('20');
    expect((screen.getByLabelText('Start date') as HTMLInputElement).value).toBe('2026-01-15');
    expect((screen.getByLabelText('Currently taking') as HTMLInputElement).checked).toBe(true);

    const link = screen.getByRole('link', { name: /visit/i });
    expect(link.getAttribute('href')).toContain('/health/history/encounter?id=3');
    expect(screen.getByRole('heading', { name: 'Documents' })).toBeTruthy();
  });

  it('sends only the fields that changed, an emptied one as null', async () => {
    render(Page);
    await screen.findByLabelText('Name');
    await fireEvent.input(screen.getByLabelText('Dose'), { target: { value: '40' } });
    await fireEvent.input(screen.getByLabelText('Reason'), { target: { value: '' } });
    await fireEvent.submit(form());

    await waitFor(() => expect(api.updateMedication).toHaveBeenCalledTimes(1));
    expect(api.updateMedication).toHaveBeenCalledWith(7, { dose: '40', reason: null });
    await waitFor(() => expect(api.getMedication).toHaveBeenCalledTimes(2));
  });

  it('asks for an end date before saving a stop', async () => {
    render(Page);
    await screen.findByLabelText('Name');
    await fireEvent.click(screen.getByLabelText('Currently taking'));
    await fireEvent.submit(form());

    expect(await screen.findByText('Enter the date you stopped taking it.')).toBeTruthy();
    expect(api.updateMedication).not.toHaveBeenCalled();

    await fireEvent.input(screen.getByLabelText('End date'), {
      target: { value: '2026-04-02' },
    });
    await fireEvent.submit(form());
    await waitFor(() =>
      expect(api.updateMedication).toHaveBeenCalledWith(7, {
        active: false,
        end_date: '2026-04-02',
      }),
    );
  });

  it('saves a row already stopped with no end date without asking for one', async () => {
    api.getMedication.mockResolvedValue({
      medication: med({ active: false, end_date: null }),
      encounter: null,
      documents: [],
    });
    render(Page);
    await screen.findByLabelText('Name');
    await fireEvent.input(screen.getByLabelText('Notes'), { target: { value: 'from 2019' } });
    await fireEvent.submit(form());
    await waitFor(() =>
      expect(api.updateMedication).toHaveBeenCalledWith(7, { notes: 'from 2019' }),
    );
    expect(screen.queryByText('Enter the date you stopped taking it.')).toBeNull();
  });

  it('undoes a stop by clearing its end date', async () => {
    api.getMedication.mockResolvedValue({
      medication: med({ active: false, end_date: '2026-03-01' }),
      encounter: null,
      documents: [],
    });
    render(Page);
    await screen.findByLabelText('Name');
    await fireEvent.click(screen.getByLabelText('Currently taking'));
    await fireEvent.submit(form());
    await waitFor(() =>
      expect(api.updateMedication).toHaveBeenCalledWith(7, { active: true, end_date: null }),
    );
  });

  it('shows a refused save beside the form and keeps the edits', async () => {
    api.updateMedication.mockRejectedValue(new Error('end_date is before start_date'));
    render(Page);
    await screen.findByLabelText('Name');
    await fireEvent.input(screen.getByLabelText('End date'), { target: { value: '2025-01-01' } });
    await fireEvent.submit(form());
    expect(await screen.findByText('end_date is before start_date')).toBeTruthy();
    expect((screen.getByLabelText('End date') as HTMLInputElement).value).toBe('2025-01-01');
  });

  it('deletes behind a confirmation and returns to the list', async () => {
    render(Page);
    await screen.findByLabelText('Name');
    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(api.deleteMedication).toHaveBeenCalledWith(7));
    await waitFor(() =>
      expect(nav.goto).toHaveBeenCalledWith(expect.stringMatching(/\/health\/medications$/)),
    );
  });

  it('reports a failed delete and stays on the page', async () => {
    api.deleteMedication.mockRejectedValue(new Error('medication not found'));
    render(Page);
    await screen.findByLabelText('Name');
    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('medication not found'));
    expect(nav.goto).not.toHaveBeenCalled();
  });
});
