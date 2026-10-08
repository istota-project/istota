import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import CredentialBackupCard from './CredentialBackupCard.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (original) => ({
  ...(await original<typeof import('$lib/api')>()),
  startStepUp: vi.fn(),
  getCredentialBackup: vi.fn(),
  setCredentialBackup: vi.fn(),
}));
import { startStepUp, getCredentialBackup, setCredentialBackup } from '$lib/api';
beforeEach(() => {
  vi.mocked(getCredentialBackup).mockResolvedValue({
    recipient_suffix: null,
    last_run: null,
    interval: 86400,
    available: true,
  });
  vi.mocked(startStepUp).mockResolvedValue({
    request_id: 'request',
    email_hint: 'a•••@example.com',
    expires_at: 'later',
  });
  vi.mocked(setCredentialBackup).mockResolvedValue({ recipient_suffix: 'abcdefgh' });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it('sets and clears a recipient only after step-up confirmation', async () => {
  render(CredentialBackupCard);
  await screen.findByText(/Add an age public key/);
  await fireEvent.input(
    screen.getByLabelText('Age public key', { exact: false, selector: 'input' }),
    {
      target: { value: 'age1fixture' },
    },
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Save backup key' }));
  await screen.findByText(/a•••@example.com/);
  expect(setCredentialBackup).not.toHaveBeenCalled();
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(setCredentialBackup).toHaveBeenCalledWith('age1fixture', {
      request_id: 'request',
      code: '123456',
    }),
  );
  await screen.findByText(/abcdefgh/);
  expect(
    (
      screen.getByLabelText('Age public key', {
        exact: false,
        selector: 'input',
      }) as HTMLInputElement
    ).value,
  ).toBe('');
  vi.mocked(setCredentialBackup).mockResolvedValue({ recipient_suffix: null });
  await fireEvent.click(screen.getByRole('button', { name: 'Turn off backups' }));
  await screen.findByText(/a•••@example.com/);
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '654321' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(setCredentialBackup).toHaveBeenLastCalledWith(null, {
      request_id: 'request',
      code: '654321',
    }),
  );
});
it('shows a missing dependency and disables registration', async () => {
  vi.mocked(getCredentialBackup).mockResolvedValue({
    recipient_suffix: null,
    last_run: null,
    interval: 86400,
    available: false,
  });
  render(CredentialBackupCard);
  await screen.findByText(/Ask the operator to enable credential backups/);
  expect(
    (screen.getByRole('button', { name: 'Save backup key' }) as HTMLButtonElement).disabled,
  ).toBe(true);
});
