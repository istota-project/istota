import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import Harness from './ConfirmDialog.harness.svelte';
import { pressEnter } from '$lib/test/implicitSubmit';

afterEach(cleanup);

function deferred() {
  let resolve!: () => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('ConfirmDialog', () => {
  it('closes once a synchronous confirm returns', async () => {
    const onConfirm = vi.fn();
    render(Harness, { onConfirm });
    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(onConfirm).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByTestId('open')).toHaveTextContent('false'));
  });

  it('shows progress and holds the dialog while the confirm is pending, then closes', async () => {
    const pending = deferred();
    const onCancel = vi.fn();
    render(Harness, { onConfirm: () => pending.promise, onCancel });
    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    const busy = await screen.findByRole('button', { name: 'Working…' });
    expect(busy).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-busy', 'true');
    await fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByTestId('open')).toHaveTextContent('true');
    pending.resolve();
    await waitFor(() => expect(screen.getByTestId('open')).toHaveTextContent('false'));
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('keeps the dialog open with the message when the confirm rejects', async () => {
    const onConfirm = vi
      .fn()
      .mockRejectedValueOnce(new Error('The card could not be removed.'))
      .mockResolvedValue(undefined);
    render(Harness, { onConfirm });
    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The card could not be removed.');
    expect(alert).toHaveClass('form-error');
    expect(screen.getByTestId('open')).toHaveTextContent('true');
    // A second try clears the old message and, resolving, closes.
    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.getByTestId('open')).toHaveTextContent('false'));
  });

  it('stays open, silently, when the confirm resolves false', async () => {
    render(Harness, { onConfirm: async () => false });
    await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled());
    expect(screen.getByTestId('open')).toHaveTextContent('true');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows a caller-held error', () => {
    render(Harness, { error: 'Could not start.' });
    expect(screen.getByRole('alert')).toHaveTextContent('Could not start.');
  });

  it('keeps the challenge gate apart from the pending state, and a rejection keeps the typed name', async () => {
    const onConfirm = vi.fn().mockRejectedValue(new Error('Refused'));
    render(Harness, { challenge: 'garden', onConfirm });
    const confirm = screen.getByRole('button', { name: 'Delete' });
    expect(confirm).toBeDisabled();
    const field = screen.getByRole('textbox', { name: 'Type to confirm' }) as HTMLInputElement;
    // Enter with the challenge unmet does nothing: the submit button is disabled.
    await pressEnter(field);
    expect(onConfirm).not.toHaveBeenCalled();
    await fireEvent.input(field, { target: { value: 'garden' } });
    expect(confirm).toBeEnabled();
    await pressEnter(field);
    expect(onConfirm).toHaveBeenCalledOnce();
    await screen.findByText('Refused');
    expect(field.value).toBe('garden');
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });

  it('honours confirmDisabled as "not yet allowed"', async () => {
    const onConfirm = vi.fn();
    render(Harness, { confirmDisabled: true, onConfirm });
    const confirm = screen.getByRole('button', { name: 'Delete' });
    expect(confirm).toBeDisabled();
    await fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('cancels from the Cancel button and on Escape', async () => {
    const onCancel = vi.fn();
    render(Harness, { onCancel });
    await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    await waitFor(() => expect(onCancel).toHaveBeenCalledOnce());
    expect(screen.getByTestId('open')).toHaveTextContent('false');
  });

  it('draws a secondary action between Cancel and the confirm, with the same async contract', async () => {
    const pending = deferred();
    const onSecondary = vi.fn(() => pending.promise);
    const onConfirm = vi.fn();
    render(Harness, { secondaryLabel: 'Disconnect, keep sharing', onSecondary, onConfirm });
    const buttons = screen.getAllByRole('button').map((b) => b.textContent?.trim());
    expect(buttons).toEqual(['Cancel', 'Disconnect, keep sharing', 'Delete']);
    expect(screen.getByRole('button', { name: 'Disconnect, keep sharing' })).toHaveAttribute(
      'type',
      'button',
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Disconnect, keep sharing' }));
    expect(onSecondary).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    pending.reject(new Error('Could not stop sharing'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not stop sharing');
    expect(screen.getByTestId('open')).toHaveTextContent('true');
  });
});
