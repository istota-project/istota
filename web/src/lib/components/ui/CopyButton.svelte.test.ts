import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';

const clipboard = vi.hoisted(() => ({ copyText: vi.fn(async () => true) }));
vi.mock('$lib/clipboard', () => clipboard);

import CopyButton from './CopyButton.svelte';
import CopyField from './CopyField.svelte';

afterEach(() => {
  cleanup();
  clipboard.copyText.mockClear();
});

describe('CopyButton', () => {
  it('copies its text through copyText, named by its label', async () => {
    render(CopyButton, { text: 'abc123', label: 'Copy token' });
    await fireEvent.click(screen.getByRole('button', { name: 'Copy token' }));
    expect(clipboard.copyText).toHaveBeenCalledWith('abc123');
  });

  it('reads a function at click time, not at render', async () => {
    let current = 'first';
    render(CopyButton, { text: () => current });
    current = 'second';
    await fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(clipboard.copyText).toHaveBeenCalledWith('second');
  });

  it('shows its label as text in the button variant', () => {
    render(CopyButton, { text: 'x', label: 'Copy password', variant: 'button' });
    expect(screen.getByRole('button', { name: 'Copy password' })).toHaveTextContent(
      'Copy password',
    );
  });
});

describe('CopyField', () => {
  it('is a read-only input named by its caption, with the copy button in its box', async () => {
    render(CopyField, { label: 'Webhook URL', value: 'https://example.com/hook' });
    const input = screen.getByLabelText('Webhook URL');
    expect(input).toHaveValue('https://example.com/hook');
    expect(input).toHaveAttribute('readonly');
    const button = screen.getByRole('button', { name: 'Copy Webhook URL' });
    expect(input.closest('.ui-input-box')).toContainElement(button);
    await fireEvent.click(button);
    expect(clipboard.copyText).toHaveBeenCalledWith('https://example.com/hook');
  });
});
