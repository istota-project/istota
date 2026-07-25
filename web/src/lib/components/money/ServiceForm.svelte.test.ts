import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import ServiceForm from './ServiceForm.svelte';
import type { ServiceRow } from '$lib/money/api';

afterEach(cleanup);

function row(overrides: Partial<ServiceRow> = {}): ServiceRow {
  return {
    key: 'consulting',
    display_name: 'Consulting',
    rate: 150,
    type: 'hours',
    income_account: 'Income:Consulting',
    ...overrides,
  };
}

function mount(props: Record<string, unknown> = {}) {
  return render(ServiceForm, {
    props: { onSave: vi.fn(), onCancel: vi.fn(), ...props } as any,
  });
}

describe('ServiceForm', () => {
  it('sends the typed rate', async () => {
    const onSave = vi.fn();
    mount({ service: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('150'), { target: { value: '200' } });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenCalledWith('consulting', expect.objectContaining({ rate: 200 }));
  });

  it('omits a blank rate rather than storing zero', async () => {
    // The invoice list rebuilds totals from live config, so a zeroed rate
    // silently reprices every past invoice carrying this service to nothing.
    const onSave = vi.fn();
    mount({ service: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('150'), { target: { value: '' } });
    await fireEvent.click(screen.getByText('Save'));
    const [, payload] = onSave.mock.calls[0];
    expect('rate' in payload).toBe(false);
  });

  it('omits the rate entirely for a per-entry service', async () => {
    const onSave = vi.fn();
    mount({ service: row({ type: 'other' }), onSave });
    await fireEvent.click(screen.getByText('Save'));
    const [, payload] = onSave.mock.calls[0];
    expect('rate' in payload).toBe(false);
  });

  it('refuses a negative rate before it reaches the server', async () => {
    const onSave = vi.fn();
    mount({ service: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('150'), { target: { value: '-5' } });
    expect(screen.getByText('Expected an amount of 0 or more')).toBeTruthy();
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('surfaces a legacy type instead of silently showing Hourly', () => {
    // A service migrated from legacy TOML can carry a type outside the set —
    // `entry_line_item` has no branch for it, so it billed as hours.
    mount({ service: row({ type: 'hourly' }) });
    expect(screen.getByText(/hourly.*unrecognised/)).toBeTruthy();
    expect(screen.getByText(/bills as hours/)).toBeTruthy();
  });

  it('says nothing about types for a conforming service', () => {
    mount({ service: row({ type: 'flat' }) });
    expect(screen.queryByText(/unrecognised/)).toBeNull();
  });

  it('rejects a malformed new key before it reaches the server', async () => {
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.input(screen.getByPlaceholderText('consulting'), {
      target: { value: 'has space' },
    });
    expect(screen.getByText('Letters, digits, - and _ only')).toBeTruthy();
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('treats the key as immutable when editing', () => {
    mount({ service: row() });
    expect(screen.queryByPlaceholderText('consulting')).toBeNull();
    expect(screen.getByText('consulting')).toBeTruthy();
  });
});
