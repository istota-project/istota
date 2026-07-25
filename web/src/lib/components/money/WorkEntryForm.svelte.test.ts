import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import WorkEntryForm from './WorkEntryForm.svelte';
import type { WorkEntryRow, ClientRow, ServiceRow } from '$lib/money/api';

afterEach(cleanup);

const SERVICES: ServiceRow[] = [
  { key: 'dev', display_name: 'Development', rate: 150, type: 'hours', income_account: '' },
  { key: 'design', display_name: 'Design', rate: 1200, type: 'days', income_account: '' },
  { key: 'retainer', display_name: 'Retainer', rate: 2000, type: 'flat', income_account: '' },
  { key: 'expenses', display_name: 'Expenses', rate: 0, type: 'other', income_account: '' },
];

const CLIENTS: ClientRow[] = [
  {
    key: 'acme',
    name: 'Acme Corp',
    email: '',
    address: '',
    terms: 30,
    entity: 'main',
    entity_name: 'Main',
    schedule: 'on-demand',
    schedule_day: 1,
    ar_account: '',
  },
];

function row(overrides: Partial<WorkEntryRow> = {}): WorkEntryRow {
  return {
    uid: 'w1',
    index: 1,
    etag: 'abc123',
    date: '2026-03-01',
    client: 'acme',
    client_name: 'Acme Corp',
    service: 'dev',
    service_name: 'Development',
    service_type: 'hours',
    qty: 3,
    amount: null,
    discount: 0,
    description: 'API work',
    entity: 'main',
    invoice: '',
    paid_date: null,
    computed_amount: 450,
    editable: true,
    warnings: [],
    ...overrides,
  };
}

function mount(props: Record<string, unknown> = {}) {
  return render(WorkEntryForm, {
    props: {
      clients: CLIENTS,
      services: SERVICES,
      onSave: vi.fn(),
      onCancel: vi.fn(),
      ...props,
    } as any,
  });
}

describe('WorkEntryForm', () => {
  it('titles itself for the add case', () => {
    mount();
    expect(screen.getByText('Add work entry')).toBeTruthy();
  });

  it('titles itself for the edit case and seeds the fields', () => {
    mount({ entry: row() });
    expect(screen.getByText('Edit work entry')).toBeTruthy();
    expect(screen.getByDisplayValue('2026-03-01')).toBeTruthy();
    expect(screen.getByDisplayValue('3')).toBeTruthy();
    expect(screen.getByDisplayValue('API work')).toBeTruthy();
  });

  it('shows the computed line item for an hourly service', () => {
    mount({ entry: row() });
    expect(screen.getByText('3 × $150.00 = $450.00')).toBeTruthy();
  });

  it('subtracts the discount in the preview', () => {
    mount({ entry: row({ discount: 50 }) });
    expect(screen.getByText('3 × $150.00 − $50.00 = $400.00')).toBeTruthy();
  });

  it('prices a days service off the day rate', () => {
    mount({ entry: row({ service: 'design', service_type: 'days', qty: 1.5 }) });
    expect(screen.getByText('1.5 × $1,200.00 = $1,800.00')).toBeTruthy();
  });

  it('ignores quantity on a flat service and says so', () => {
    mount({ entry: row({ service: 'retainer', service_type: 'flat', qty: 99 }) });
    expect(screen.getByText('$2,000.00 = $2,000.00')).toBeTruthy();
    expect(screen.getByText(/Flat-rate service/)).toBeTruthy();
  });

  it('asks for an amount rather than a quantity on an "other" service', () => {
    mount({ entry: row({ service: 'expenses', service_type: 'other', qty: null, amount: 320 }) });
    expect(screen.getByText('Amount')).toBeTruthy();
    expect(screen.queryByText('Hours')).toBeNull();
    expect(screen.getByText('1 × $320.00 = $320.00')).toBeTruthy();
  });

  it('labels the quantity field per service type', () => {
    mount({ entry: row() });
    expect(screen.getByText('Hours')).toBeTruthy();
    cleanup();
    mount({ entry: row({ service: 'design', service_type: 'days' }) });
    expect(screen.getByText('Days')).toBeTruthy();
  });

  it('leaves a quantity field it never rendered alone', async () => {
    // The form shows no Hours/Amount box for a flat service, so it has no
    // user input for either — sending null would wipe stored values the user
    // never saw, let alone edited.
    const onSave = vi.fn();
    mount({ entry: row({ service: 'retainer', service_type: 'flat', qty: 99 }), onSave });
    await fireEvent.click(screen.getByText('Save'));

    expect(onSave).toHaveBeenCalledTimes(1);
    const payload = onSave.mock.calls[0][0];
    expect(payload.qty).toBeUndefined();
    expect(payload.amount).toBeUndefined();
    expect(payload.service).toBe('retainer');
  });

  it('preserves an amount on an hours entry priced by the amount fallback', async () => {
    // `entry_line_item` falls back to `amount` when an hours/days entry has no
    // qty (the `work add -a 500 -s dev` shape). The form prices it correctly
    // in the preview, so saving must not zero it.
    const onSave = vi.fn();
    mount({ entry: row({ qty: null, amount: 500 }), onSave });
    expect(screen.getByText('1 × $500.00 = $500.00')).toBeTruthy();

    await fireEvent.click(screen.getByText('Save'));
    expect(onSave.mock.calls[0][0].amount).toBeUndefined();
  });

  it('preserves an amount when the service list failed to load', async () => {
    // loadConfig is best-effort: on failure every entry reads as the default
    // hours type, which used to make an edit null out an `other` entry's amount.
    const onSave = vi.fn();
    mount({
      services: [],
      entry: row({ service: 'expenses', service_type: 'other', qty: null, amount: 320 }),
      onSave,
    });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave.mock.calls[0][0].amount).toBeUndefined();
  });

  it('parses numeric fields out of the text inputs', async () => {
    const onSave = vi.fn();
    mount({ entry: row({ qty: null, discount: 0 }), onSave });

    await fireEvent.input(screen.getByPlaceholderText('e.g. 3'), { target: { value: '7.5' } });
    await fireEvent.input(screen.getByPlaceholderText('0'), { target: { value: '25' } });
    await fireEvent.click(screen.getByText('Save'));

    expect(onSave.mock.calls[0][0]).toMatchObject({ qty: 7.5, discount: 25 });
  });

  it('treats a blank quantity as null rather than zero', async () => {
    const onSave = vi.fn();
    mount({ entry: row({ qty: null }), onSave });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave.mock.calls[0][0].qty).toBeNull();
  });

  it('will not save without a client', async () => {
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('renders a server error', () => {
    mount({ entry: row(), error: 'unknown service: ghost' });
    expect(screen.getByText('unknown service: ghost')).toBeTruthy();
  });

  it('disables save while in flight', () => {
    mount({ entry: row(), saving: true });
    expect((screen.getByText('Saving…') as HTMLButtonElement).disabled).toBe(true);
  });

  it('cancels', async () => {
    const onCancel = vi.fn();
    mount({ onCancel });
    await fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalled();
  });
});

describe('WorkEntryForm keyboard', () => {
  it('saves on Enter from a text field', async () => {
    const onSave = vi.fn();
    mount({ entry: row(), onSave });
    await fireEvent.keyDown(screen.getByPlaceholderText('e.g. 3'), { key: 'Enter' });
    expect(onSave).toHaveBeenCalled();
  });

  it('does not save on Enter from the description field', async () => {
    const onSave = vi.fn();
    mount({ entry: row(), onSave });
    await fireEvent.keyDown(screen.getByPlaceholderText('Optional'), { key: 'Enter' });
    expect(onSave).not.toHaveBeenCalled();
  });

  it('does not save on Enter from a dropdown', async () => {
    // Confirming a Select option must not select *and* commit in one
    // keystroke — that wrote a half-filled entry.
    const onSave = vi.fn();
    mount({ entry: row(), onSave });
    const trigger = screen.getByLabelText('Service');
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe('WorkEntryForm default date', () => {
  it('defaults to the local date, not the UTC one', () => {
    // Pick an hour that falls on a different UTC *day* than local, whichever
    // side of Greenwich the runner sits on: late evening for a western
    // offset, early morning for an eastern one.
    const offsetMinutes = new Date(2026, 2, 1).getTimezoneOffset();
    const hour = offsetMinutes > 0 ? 23 : 1;

    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date(2026, 2, 1, hour, 30, 0));
      if (offsetMinutes !== 0) {
        // Guard the guard: this only tests anything if the two disagree.
        expect(new Date().toISOString().slice(0, 10)).not.toBe('2026-03-01');
      }
      mount();
      expect(screen.getByDisplayValue('2026-03-01')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});
