import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import SecurityCard from './SecurityCard.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));

vi.mock('$lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api')>()),
  changePassword: vi.fn(),
}));
import { changePassword, AuthError } from '$lib/api';

const auth = { method: 'email' as const, email: 'alice@example.com', can_change_password: true };
afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

it('requires the current password and ends the session after a successful change', async () => {
  const onSignedOut = vi.fn();
  vi.mocked(changePassword).mockResolvedValue({ signed_out: true });
  render(SecurityCard, { auth, onSignedOut });
  expect(screen.getByText(/signs you out of every session/i)).toBeTruthy();
  expect(screen.getByText(auth.email)).toBeTruthy();
  await fireEvent.input(screen.getByLabelText('Current password'), {
    target: { value: 'old passphrase' },
  });
  await fireEvent.input(screen.getByLabelText('New password'), {
    target: { value: 'new passphrase' },
  });
  await fireEvent.input(screen.getByLabelText('Confirm new password'), {
    target: { value: 'new passphrase' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Change password' }));
  await waitFor(() => expect(onSignedOut).toHaveBeenCalledOnce());
  expect(changePassword).toHaveBeenCalledWith('old passphrase', 'new passphrase');
});

it('shows passwordless users their login email and the reset-page action', () => {
  render(SecurityCard, { auth: { ...auth, can_change_password: false }, onSignedOut: vi.fn() });
  expect(screen.queryByLabelText('Current password')).toBeNull();
  expect(screen.getByRole('link', { name: 'Set a password' }).getAttribute('href')).toBe(
    '/istota/auth/reset',
  );
  expect(screen.getByText(auth.email)).toBeTruthy();
});

it('does not show the card for a Nextcloud session', () => {
  render(SecurityCard, { auth: { ...auth, method: 'nextcloud' }, onSignedOut: vi.fn() });
  expect(screen.queryByRole('heading', { name: 'Security' })).toBeNull();
});

it('keeps errors visible and refuses mismatched confirmation', async () => {
  const onSignedOut = vi.fn();
  render(SecurityCard, { auth, onSignedOut });
  await fireEvent.input(screen.getByLabelText('Current password'), {
    target: { value: 'old passphrase' },
  });
  await fireEvent.input(screen.getByLabelText('New password'), {
    target: { value: 'new passphrase' },
  });
  await fireEvent.input(screen.getByLabelText('Confirm new password'), {
    target: { value: 'different' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Change password' }));
  expect(screen.getByRole('alert').textContent).toContain('Passwords must match');
  expect(changePassword).not.toHaveBeenCalled();
  await fireEvent.input(screen.getByLabelText('Confirm new password'), {
    target: { value: 'new passphrase' },
  });
  vi.mocked(changePassword).mockRejectedValueOnce(new Error('Current password was not accepted.'));
  await fireEvent.click(screen.getByRole('button', { name: 'Change password' }));
  await waitFor(() =>
    expect(screen.getByRole('alert').textContent).toContain('Current password was not accepted.'),
  );
  expect(onSignedOut).not.toHaveBeenCalled();
  vi.mocked(changePassword).mockRejectedValueOnce(new AuthError());
  await fireEvent.click(screen.getByRole('button', { name: 'Change password' }));
  await waitFor(() => expect(onSignedOut).toHaveBeenCalledOnce());
});
