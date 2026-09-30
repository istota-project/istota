import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import CredentialGrantsCard from './CredentialGrantsCard.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api')>()),
  getCredentialGrants: vi.fn(),
  saveCredentialGrant: vi.fn(),
  grantExistingCredentials: vi.fn(),
  revokeCredentialGrant: vi.fn(),
}));
import { getCredentialGrants, saveCredentialGrant, grantExistingCredentials } from '$lib/api';
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it('shows binding metadata and saves narrow defaults without scheduled access', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue({
    credentials: [
      {
        name: 'portal',
        source: 'vault',
        hosts: ['portal.example'],
        headers: ['authorization'],
        revealable: false,
        grant: null,
      },
    ],
    rooms: [],
    grant_existing_available: true,
    sandboxed: false,
  });
  vi.mocked(saveCredentialGrant).mockResolvedValue({ ok: true });
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  expect(screen.getByText('Ungranted')).toBeTruthy();
  expect(screen.getByText(/values are not contained/i)).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Edit grant for portal' }));
  expect((screen.getByLabelText('Allow scheduled tasks') as HTMLInputElement).checked).toBe(false);
  await fireEvent.click(screen.getByRole('button', { name: 'Save grant' }));
  await waitFor(() =>
    expect(saveCredentialGrant).toHaveBeenCalledWith('portal', {
      scope_mode: 'all',
      rooms: [],
      methods: ['GET', 'HEAD', 'POST', 'PUT', 'PATCH'],
      allow_scheduled: false,
    }),
  );
});

it('requires confirmation for grant-existing and keeps a failed save visible', async () => {
  vi.mocked(getCredentialGrants).mockResolvedValue({
    credentials: [
      {
        name: 'portal',
        source: 'vault',
        hosts: ['portal.example'],
        headers: ['authorization'],
        revealable: false,
        grant: null,
      },
    ],
    rooms: [],
    grant_existing_available: true,
    sandboxed: true,
  });
  vi.mocked(grantExistingCredentials).mockResolvedValue({ ok: true, count: 1 });
  render(CredentialGrantsCard);
  await screen.findByText('portal.example');
  await fireEvent.click(screen.getByRole('button', { name: 'Grant what exists' }));
  expect(grantExistingCredentials).not.toHaveBeenCalled();
  const dialog = screen.getByRole('dialog');
  await fireEvent.click(dialog.querySelector('.btn-primary')!);
  await waitFor(() => expect(grantExistingCredentials).toHaveBeenCalledOnce());
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await fireEvent.click(screen.getByRole('button', { name: 'Edit grant for portal' }));
  vi.mocked(saveCredentialGrant).mockRejectedValueOnce(new Error('Could not save policy'));
  await fireEvent.click(screen.getByRole('button', { name: 'Save grant' }));
  await waitFor(() =>
    expect(screen.getByRole('dialog').textContent).toContain('Could not save policy'),
  );
});
