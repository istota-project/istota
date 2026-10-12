import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
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

describe('required', () => {
  it('passes through to the input', () => {
    mount({ required: true });
    expect((screen.getByLabelText('Token') as HTMLInputElement).required).toBe(true);
  });
});
