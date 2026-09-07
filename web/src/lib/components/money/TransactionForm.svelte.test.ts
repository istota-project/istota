import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import TransactionForm from './TransactionForm.svelte';
import type { TransactionRow } from '$lib/money/api';

afterEach(cleanup);

function row(overrides: Partial<TransactionRow> = {}): TransactionRow {
  return {
    date: '2026-03-01',
    payee: 'Acme Corp',
    narration: 'API work',
    account: 'Expenses:Software',
    position: '-12.50 USD',
    ...overrides,
  } as TransactionRow;
}

function mount(props: Record<string, unknown> = {}) {
  return render(TransactionForm, {
    props: {
      txn: row(),
      accounts: ['Expenses:Software', 'Expenses:Travel'],
      onSave: vi.fn(),
      onCancel: vi.fn(),
      ...props,
    } as any,
  });
}

describe('TransactionForm keyboard', () => {
  it('saves on Enter from a text field', async () => {
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.keyDown(screen.getByPlaceholderText('e.g. Acme Corp'), { key: 'Enter' });
    expect(onSave).toHaveBeenCalled();
  });

  it('does not save on Enter from the account dropdown', async () => {
    // Confirming a Select option must not select *and* commit in one keystroke.
    // Every other money form guards this; this one had no guard at all.
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.keyDown(screen.getByLabelText('Account'), { key: 'Enter' });
    expect(onSave).not.toHaveBeenCalled();
  });

  it('ignores a key that is not Enter', async () => {
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.keyDown(screen.getByPlaceholderText('e.g. Acme Corp'), { key: 'a' });
    expect(onSave).not.toHaveBeenCalled();
  });
});
