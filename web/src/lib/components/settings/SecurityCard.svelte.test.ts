import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import SecurityCard from './SecurityCard.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));

vi.mock('$lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api')>()),
  changePassword: vi.fn(),
}));
import { changePassword, AuthError } from '$lib/api';

const auth = {
  method: 'email' as const,
  email: 'alice@example.com',
  can_change_password: true,
  can_set_password: false,
};
afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

it('requires the current password and ends the session after a successful change', async () => {
  const onSignedOut = vi.fn();
  vi.mocked(changePassword).mockResolvedValue({ signed_out: true });
  render(SecurityCard, { auth, onSignedOut });
  expect(screen.getByText(/signs you out of every session/i)).toHaveClass('description');
  expect(screen.getByLabelText('Login email')).toHaveValue(auth.email);
  expect(screen.getByLabelText('Login email')).toHaveAttribute('readonly');
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
  render(SecurityCard, {
    auth: { ...auth, can_change_password: false, can_set_password: true },
    onSignedOut: vi.fn(),
  });
  expect(screen.queryByLabelText('Current password')).toBeNull();
  expect(screen.getByRole('link', { name: 'Set a password' }).getAttribute('href')).toBe(
    '/istota/auth/reset',
  );
  expect(screen.getByLabelText('Login email')).toHaveValue(auth.email);
  expect(screen.getByText(/no password yet/)).toHaveClass('description');
  expect(screen.getByRole('link', { name: 'Set a password' }).parentElement).toHaveClass('actions');
});

it('offers a Nextcloud session with no password the set-password action', () => {
  render(SecurityCard, {
    auth: { ...auth, method: 'nextcloud', can_change_password: false, can_set_password: true },
    onSignedOut: vi.fn(),
  });
  expect(screen.queryByLabelText('Current password')).toBeNull();
  expect(screen.getByRole('link', { name: 'Set a password' })).toBeTruthy();
});

it('does not show the card when the session can neither set nor change a password', () => {
  render(SecurityCard, {
    auth: { ...auth, method: 'nextcloud', email: null, can_change_password: false },
    onSignedOut: vi.fn(),
  });
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
  // A mismatch is the confirmation field's error, beside it, not a banner.
  const confirmField = screen.getByLabelText('Confirm new password');
  expect(confirmField).toHaveAttribute('aria-invalid', 'true');
  expect(confirmField).toHaveAccessibleDescription(/Passwords must match/);
  expect(screen.queryByRole('alert')).toBeNull();
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
