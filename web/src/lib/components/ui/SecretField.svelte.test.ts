import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import SecretField from './SecretField.svelte';

afterEach(cleanup);

function mount(over: Record<string, unknown> = {}) {
  return render(SecretField, {
    props: { label: 'Token', configured: false, value: '', onValueChange: vi.fn(), ...over },
  });
}

describe('the clear button', () => {
  it('is absent when the caller cannot clear a stored value', () => {
    mount({ configured: true });
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('is absent while nothing is stored', () => {
    mount({ onRequestClear: vi.fn() });
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('is a labelled Clear button once a value is stored', () => {
    mount({ configured: true, onRequestClear: vi.fn() });
    const button = screen.getByRole('button', { name: 'Clear stored Token' });
    expect(button.textContent?.trim()).toBe('Clear');
  });
});

describe('clearMode', () => {
  it('immediate: Clear sits in the row beside the input and asks the caller', async () => {
    const onRequestClear = vi.fn();
    const { container } = mount({ configured: true, onRequestClear });
    const button = screen.getByRole('button', { name: 'Clear stored Token' });
    expect(container.querySelector('.secret-row')).toContainElement(button);
    await fireEvent.click(button);
    expect(onRequestClear).toHaveBeenCalledTimes(1);
  });

  it('staged: Clear sits after the field, and a pending clear offers Undo', async () => {
    const onRequestClear = vi.fn();
    const onUndoClear = vi.fn();
    const { container, rerender } = mount({
      configured: true,
      clearMode: 'staged',
      onRequestClear,
      onUndoClear,
    });
    const button = screen.getByRole('button', { name: 'Clear stored Token' });
    expect(container.querySelector('.field')).not.toContainElement(button);
    await fireEvent.click(button);
    expect(onRequestClear).toHaveBeenCalledTimes(1);

    await rerender({ pendingClear: true });
    expect(screen.getByText('Will be cleared.')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(onUndoClear).toHaveBeenCalledTimes(1);
  });
});

describe('status and read-only', () => {
  it('shows where the value is held as a badge after the label', () => {
    mount({ configured: true, status: { label: 'In config.toml', variant: 'warn' } });
    expect(screen.getByText('In config.toml')).toHaveClass('badge-warn');
  });

  it('shows the reason in place of the input when it cannot be changed here', () => {
    mount({ configured: true, readOnlyReason: 'Set by the deployment', onRequestClear: vi.fn() });
    expect(screen.getByText('Set by the deployment')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(document.querySelector('input')).toBeNull();
    expect(screen.queryByRole('button', { name: /Clear/ })).toBeNull();
  });
});

describe('required', () => {
  it('passes through to the input', () => {
    mount({ required: true });
    expect((screen.getByLabelText('Token') as HTMLInputElement).required).toBe(true);
  });
});
