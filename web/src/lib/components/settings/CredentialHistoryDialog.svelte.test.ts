import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import CredentialHistoryDialog from './CredentialHistoryDialog.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (original) => ({
  ...(await original<typeof import('$lib/api')>()),
  getCredentialHistory: vi.fn(),
  startStepUp: vi.fn(),
  restoreCredentialHistory: vi.fn(),
}));
import { getCredentialHistory, startStepUp, restoreCredentialHistory } from '$lib/api';
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it('lists fields without values and restores only with a code', async () => {
  vi.mocked(getCredentialHistory).mockResolvedValue([
    {
      id: 7,
      name: 'example',
      op: 'delete',
      actor: 'web:alice',
      at: '2026-01-01',
      fields: ['example', 'example_username'],
    },
  ]);
  vi.mocked(startStepUp).mockResolvedValue({
    request_id: 'r',
    expires_at: 'later',
    email_hint: 'a•••@example.com',
  });
  vi.mocked(restoreCredentialHistory).mockResolvedValue({ restored: ['example'] });
  const changed = vi.fn();
  render(CredentialHistoryDialog, { name: 'example', onClose: vi.fn(), onChanged: changed });
  await screen.findByText('example, example_username');
  await fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
  expect(restoreCredentialHistory).not.toHaveBeenCalled();
  await screen.findByText(/a•••@example.com/);
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(restoreCredentialHistory).toHaveBeenCalledWith(7, { request_id: 'r', code: '123456' }),
  );
  await waitFor(() => expect(changed).toHaveBeenCalled());
});
