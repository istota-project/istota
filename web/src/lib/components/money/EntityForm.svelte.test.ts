import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import EntityForm from './EntityForm.svelte';
import type { EntityRow } from '$lib/money/api';

afterEach(cleanup);

function row(overrides: Partial<EntityRow> = {}): EntityRow {
  return {
    key: 'main',
    name: 'Main LLC',
    address: '1 Main St',
    email: 'billing@main.example',
    payment_instructions: 'Wire to…',
    logo: '',
    ar_account: 'Assets:Accounts-Receivable',
    bank_account: 'Assets:Bank:Checking',
    currency: 'USD',
    ...overrides,
  };
}

function mount(props: Record<string, unknown> = {}) {
  return render(EntityForm, {
    props: { onSave: vi.fn(), onCancel: vi.fn(), ...props } as any,
  });
}

describe('EntityForm', () => {
  it('sends the edited fields under the existing key', async () => {
    const onSave = vi.fn();
    mount({ entity: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('Acme Studio LLC'), {
      target: { value: 'Main Holdings LLC' },
    });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenCalledWith(
      'main',
      expect.objectContaining({ name: 'Main Holdings LLC' }),
    );
  });

  it.each(['/etc/passwd', '../../secrets.png', '~/private.png'])(
    'refuses a logo path that escapes the accounting folder: %s',
    async (logo) => {
      // The logo is base64-embedded into the invoice, resolved against the
      // accounting folder — pathlib lets an absolute operand replace it.
      const onSave = vi.fn();
      mount({ entity: row(), onSave });
      await fireEvent.input(screen.getByPlaceholderText('invoices/logo.png'), {
        target: { value: logo },
      });
      expect(screen.getByText('Expected a path inside the accounting folder')).toBeTruthy();
      await fireEvent.click(screen.getByText('Save'));
      expect(onSave).not.toHaveBeenCalled();
    },
  );

  it('accepts a relative logo path', async () => {
    const onSave = vi.fn();
    mount({ entity: row(), onSave });
    await fireEvent.input(screen.getByPlaceholderText('invoices/logo.png'), {
      target: { value: 'invoices/logo.png' },
    });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenCalledWith(
      'main',
      expect.objectContaining({ logo: 'invoices/logo.png' }),
    );
  });

  it('rejects a malformed new key before it reaches the server', async () => {
    const onSave = vi.fn();
    mount({ onSave });
    await fireEvent.input(screen.getByPlaceholderText('main'), {
      target: { value: 'has space' },
    });
    expect(screen.getByText('Letters, digits, - and _ only')).toBeTruthy();
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('will not save without a name', async () => {
    const onSave = vi.fn();
    mount({ entity: row({ name: '' }), onSave });
    await fireEvent.click(screen.getByText('Save'));
    expect(onSave).not.toHaveBeenCalled();
  });

  it('treats the key as immutable when editing', () => {
    mount({ entity: row() });
    expect(screen.queryByPlaceholderText('main')).toBeNull();
    expect(screen.getByText('main')).toBeTruthy();
  });

  it('renders a server error', () => {
    mount({ entity: row(), error: "entity 'main' is the default entity" });
    expect(screen.getByText("entity 'main' is the default entity")).toBeTruthy();
  });
});
