import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import ClientForm from './ClientForm.svelte';
import type { ClientConfigRow, EntityRow } from '$lib/money/api';

afterEach(cleanup);

const ENTITIES: EntityRow[] = [
  {
    key: 'main',
    name: 'Main LLC',
    address: '',
    email: '',
    payment_instructions: '',
    logo: '',
    ar_account: '',
    bank_account: '',
    currency: 'USD',
  },
];

function row(overrides: Partial<ClientConfigRow> = {}): ClientConfigRow {
  return {
    key: 'acme',
    name: 'Acme Corp',
    address: '100 Acme Way',
    email: 'ap@acme.example',
    terms: 30,
    ar_account: 'Assets:AR:Acme',
    entity: 'main',
    schedule: 'on-demand',
    schedule_day: 1,
    reminder_days: 3,
    notifications: '',
    days_until_overdue: 0,
    ledger_posting: true,
    bundles: [],
    separate: [],
    ...overrides,
  };
}

function mount(props: Record<string, unknown> = {}) {
  return render(ClientForm, {
    props: {
      entities: ENTITIES,
      defaultEntity: 'main',
      onSave: vi.fn(),
      onCancel: vi.fn(),
      ...props,
    } as any,
  });
}

describe('ClientForm', () => {
  it('titles itself for the add case and asks for a key', () => {
    mount();
    expect(screen.getByText('Add client')).toBeTruthy();
    expect(screen.getByPlaceholderText('acme')).toBeTruthy();
  });

  it('renders the key as static text when editing', () => {
    // The key is the identity — work entries reference it by name — so it is
    // set on create and read-only thereafter.
    mount({ client: row() });
    expect(screen.getByText('Edit Acme Corp')).toBeTruthy();
    expect(screen.queryByPlaceholderText('acme')).toBeNull();
    expect(screen.getByDisplayValue('Acme Corp')).toBeTruthy();
  });

  it('sends the identity and the editable fields', async () => {
    const onSave = vi.fn();
    mount({ client: row(), onSave });
    await fireEvent.click(screen.getByText('Save'));

    expect(onSave).toHaveBeenCalledTimes(1);
    const [key, data] = onSave.mock.calls[0];
    expect(key).toBe('acme');
    expect(data).toMatchObject({ name: 'Acme Corp', entity: 'main', terms: 30 });
  });

  it('clears an optional field with an empty string, never null', async () => {
    // The store skips null values when merging, so a null would leave the old
    // value in place while the form showed the field as cleared.
    const onSave = vi.fn();
    mount({ client: row(), onSave });

    await fireEvent.input(screen.getByDisplayValue('Assets:AR:Acme'), {
      target: { value: '' },
    });
    await fireEvent.click(screen.getByText('Save'));

    const data = onSave.mock.calls[0][1];
    expect(data.ar_account).toBe('');
    expect(data.ar_account).not.toBeNull();
  });

  it('omits bundles and separate so the merge preserves them', async () => {
    const onSave = vi.fn();
    mount({ client: row({ separate: ['dev'], bundles: [{ name: 'B' }] }), onSave });
    await fireEvent.click(screen.getByText('Save'));

    const data = onSave.mock.calls[0][1];
    expect('separate' in data).toBe(false);
    expect('bundles' in data).toBe(false);
  });

  it('hides the schedule day until the schedule is monthly', async () => {
    mount({ client: row({ schedule: 'on-demand' }) });
    expect(screen.queryByText('Schedule day')).toBeNull();

    cleanup();
    mount({ client: row({ schedule: 'monthly', schedule_day: 15 }) });
    expect(screen.getByText('Schedule day')).toBeTruthy();
    expect(screen.getByDisplayValue('15')).toBeTruthy();
  });

  it('only sends a schedule day on a monthly schedule', async () => {
    const onSave = vi.fn();
    mount({ client: row({ schedule: 'on-demand', schedule_day: 15 }), onSave });
    await fireEvent.click(screen.getByText('Save'));
    expect('schedule_day' in onSave.mock.calls[0][1]).toBe(false);

    cleanup();
    const onSave2 = vi.fn();
    mount({ client: row({ schedule: 'monthly', schedule_day: 15 }), onSave: onSave2 });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave2.mock.calls[0][1].schedule_day).toBe(15);
  });

  it('reads terms as a day count or a label', async () => {
    const onSave = vi.fn();
    mount({ client: row({ terms: 'NET 15' }), onSave });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave.mock.calls[0][1].terms).toBe('NET 15');

    cleanup();
    const onSave2 = vi.fn();
    mount({ client: row({ terms: 30 }), onSave: onSave2 });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave2.mock.calls[0][1].terms).toBe(30);
  });

  it('leaves a blank numeric field alone rather than sending zero', async () => {
    const onSave = vi.fn();
    mount({ client: row({ reminder_days: 5 }), onSave });

    await fireEvent.input(screen.getByDisplayValue('5'), { target: { value: '' } });
    await fireEvent.click(screen.getByText('Save'));
    expect('reminder_days' in onSave.mock.calls[0][1]).toBe(false);
  });

  it('names the default entity for a client that has none of its own', () => {
    // The empty value is meaningful — "fall back to default_entity" — so it
    // has to read as a choice rather than a blank.
    mount({ client: row({ entity: '' }) });
    expect(screen.getByText('Use default (main)')).toBeTruthy();
  });

  it('will not save without a name', async () => {
    const onSave = vi.fn();
    mount({ client: row({ name: '' }), onSave });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('will not save a new client without a key', async () => {
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.input(screen.getByPlaceholderText('Acme Corp'), {
      target: { value: 'Acme' },
    });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('rejects a malformed new key before it reaches the server', async () => {
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.input(screen.getByPlaceholderText('acme'), {
      target: { value: 'has space' },
    });
    expect(screen.getByText('Letters, digits, - and _ only')).toBeTruthy();
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('renders a server error', () => {
    mount({ client: row(), error: "client 'acme' already exists" });
    expect(screen.getByText("client 'acme' already exists")).toBeTruthy();
  });

  it('disables save while in flight', () => {
    mount({ client: row(), saving: true });
    expect((screen.getByText('Saving…') as HTMLButtonElement).disabled).toBe(true);
  });

  it('cancels', async () => {
    const onCancel = vi.fn();
    mount({ onCancel });
    await fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalled();
  });

  it('lowercases a new key as you type', async () => {
    // Work entries store the client lowercased, so a mixed-case key matches
    // none of them and the client's work is silently never billed. Forcing it
    // in the field means the key you see is the key you get.
    const onSave = vi.fn();
    mount({ onSave });
    const keyInput = screen.getByPlaceholderText('acme') as HTMLInputElement;
    await fireEvent.input(keyInput, { target: { value: 'AcmeCorp' } });
    expect(keyInput.value).toBe('acmecorp');
    await fireEvent.input(screen.getByPlaceholderText('Acme Corp'), {
      target: { value: 'Acme' },
    });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenCalledWith('acmecorp', expect.anything());
  });

  it('refuses negative numeric terms', async () => {
    // The column is TEXT and the loader coerces "-5" back to -5, which renders
    // a due date before the invoice date.
    const onSave = vi.fn();
    mount({ client: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('30'), { target: { value: '-5' } });
    expect(screen.getByText('Expected 0 days or more')).toBeTruthy();
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('still accepts a label as terms', async () => {
    const onSave = vi.fn();
    mount({ client: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('30'), { target: { value: 'NET 15' } });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenCalledWith('acme', expect.objectContaining({ terms: 'NET 15' }));
  });

  it('surfaces a legacy schedule instead of silently showing on-demand', () => {
    // A client migrated from legacy TOML can carry a schedule outside the set;
    // it was simply never picked up by the scheduler.
    mount({ client: row({ schedule: 'weekly' }) });
    expect(screen.getByText(/weekly.*unrecognised/)).toBeTruthy();
    expect(screen.getByText(/never invoiced automatically/)).toBeTruthy();
  });

  it('says nothing about schedules for a conforming client', () => {
    mount({ client: row({ schedule: 'monthly' }) });
    expect(screen.queryByText(/unrecognised/)).toBeNull();
  });
});
