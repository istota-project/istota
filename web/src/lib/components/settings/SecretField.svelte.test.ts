import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import SecretField from './SecretField.svelte';

afterEach(cleanup);

function mount(over: Record<string, unknown> = {}) {
  return render(SecretField, {
    props: { label: 'Token', configured: false, value: '', onValueChange: vi.fn(), ...over },
  });
}

describe('the clear button slot', () => {
  it('is not reserved when the caller cannot clear a stored value', () => {
    const { container } = mount({ configured: true });
    expect(container.querySelector('.clear-placeholder')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('is held open while nothing is stored, for a caller that can clear', () => {
    const { container } = mount({ onRequestClear: vi.fn() });
    expect(container.querySelector('.clear-placeholder')).not.toBeNull();
  });

  it('holds the button once a value is stored', () => {
    const { container } = mount({ configured: true, onRequestClear: vi.fn() });
    expect(container.querySelector('.clear-placeholder')).toBeNull();
    expect(screen.getByRole('button', { name: 'Clear stored Token' })).toBeTruthy();
  });
});

describe('required', () => {
  it('passes through to the input', () => {
    mount({ required: true });
    expect((screen.getByLabelText('Token') as HTMLInputElement).required).toBe(true);
  });
});
