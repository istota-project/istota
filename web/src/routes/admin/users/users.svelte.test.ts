import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
import Page from './+page.svelte';

const rows = [
  {
    user_id: 'Legacy.User',
    display_name: 'Legacy',
    state: 'nextcloud_only',
    identity: null,
    is_admin: false,
  },
  {
    user_id: 'bob',
    display_name: 'Bob',
    state: 'passwordless',
    identity: { email: 'bob@example.com', disabled: false, last_login_at: null },
    is_admin: false,
  },
  {
    user_id: 'alice',
    display_name: 'Alice',
    state: 'password_set',
    identity: { email: 'alice@example.com', disabled: false, last_login_at: null },
    is_admin: true,
  },
];
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.getAdminUsers.mockResolvedValue({ users: rows, orphans: [], email_enabled: true });
  api.createAdminUser.mockResolvedValue({ sent: true });
  api.adminUserAction.mockResolvedValue({ sent: true });
});

it('explains directory validation and prevents invalid new IDs', async () => {
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Add user' }));
  const id = await screen.findByLabelText('User ID');
  expect(screen.getByText(/directory name/)).toBeTruthy();
  expect(screen.getByText(/background work.*daemon reload/)).toBeTruthy();
  await fireEvent.input(id, { target: { value: '../escape' } });
  await fireEvent.input(screen.getByLabelText('Email'), { target: { value: 'new@example.com' } });
  await fireEvent.submit(screen.getByRole('form', { name: 'Add user' }));
  expect(screen.getByRole('alert').textContent).toContain('lowercase');
  expect(api.createAdminUser).not.toHaveBeenCalled();
});

it('shows the three states and only permits attaching email to a Nextcloud-only row', async () => {
  render(Page);
  const row = await screen.findByRole('row', { name: /Legacy/ });
  expect(within(row).getByText('Nextcloud only')).toBeTruthy();
  expect(within(row).queryByRole('button', { name: 'Disable' })).toBeNull();
  expect(within(row).queryByRole('button', { name: 'Sign out everywhere' })).toBeNull();
  expect(screen.getByText('Email code')).toBeTruthy();
  expect(screen.getByText('Password set')).toBeTruthy();
  await fireEvent.click(within(row).getByRole('button', { name: 'Actions for Legacy' }));
  expect(await screen.findAllByRole('menuitem')).toHaveLength(1);
  await fireEvent.click(screen.getByRole('menuitem', { name: 'Attach email' }));
  const dialog = await screen.findByRole('dialog', { name: 'Attach email' });
  expect(within(dialog).getByLabelText('User ID')).toHaveValue('Legacy.User');
  expect(within(dialog).getByLabelText('User ID')).toHaveAttribute('readonly');
  await fireEvent.input(screen.getByLabelText('Email'), {
    target: { value: 'legacy@example.com' },
  });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Attach and invite' }));
  await waitFor(() =>
    expect(api.createAdminUser).toHaveBeenCalledWith({
      user_id: 'Legacy.User',
      email: 'legacy@example.com',
      display_name: '',
    }),
  );
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(screen.getByRole('status')).toHaveTextContent('invitation sent');
});

it('names disable and removal effects and sends links through the API', async () => {
  render(Page);
  await screen.findByRole('row', { name: /Bob/ });
  api.adminUserAction.mockRejectedValueOnce(new Error('The reset link could not be sent.'));
  await fireEvent.click(screen.getByRole('button', { name: 'Actions for Bob' }));
  // Sign-in is by a code the user requests themselves, so no admin action sends one.
  expect(screen.queryByRole('menuitem', { name: 'Send sign-in link' })).toBeNull();
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Send password reset' }));
  await waitFor(() => expect(api.adminUserAction).toHaveBeenCalledWith('bob', 'reset'));
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be sent');
});

it('keeps attach errors and entered email in the dialog', async () => {
  api.createAdminUser.mockRejectedValueOnce(new Error('The invitation could not be sent.'));
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for Legacy' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Attach email' }));
  const dialog = await screen.findByRole('dialog', { name: 'Attach email' });
  await fireEvent.input(within(dialog).getByLabelText('Email'), {
    target: { value: 'legacy@example.com' },
  });
  await fireEvent.submit(within(dialog).getByRole('form'));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('could not be sent');
  expect(within(dialog).getByLabelText('Email')).toHaveValue('legacy@example.com');
  await waitFor(() => expect(api.getAdminUsers).toHaveBeenCalledTimes(2));
});

it('cancels attachment and opens a fresh add-user dialog', async () => {
  render(Page);
  expect(screen.queryByRole('form')).toBeNull();
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for Legacy' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Attach email' }));
  await fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Add user' }));
  const dialog = await screen.findByRole('dialog', { name: 'Add user' });
  expect(within(dialog).getByLabelText('User ID')).toHaveValue('');
  expect(within(dialog).getByLabelText('User ID')).not.toHaveAttribute('readonly');
});

it('explains removal in a confirmation and only acts after confirmation', async () => {
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for Bob' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove email login' }));
  const dialog = await screen.findByRole('dialog', { name: 'Remove email login' });
  expect(dialog).toHaveTextContent('fresh Nextcloud sign-in');
  expect(dialog).toHaveTextContent('profile and user data are kept');
  expect(dialog).toHaveTextContent('Bob');
  expect(api.adminUserAction).not.toHaveBeenCalled();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await waitFor(() => expect(api.adminUserAction).toHaveBeenCalledWith('bob', 'remove'));
});

it('disables creation and attachment when email sign-in is off', async () => {
  api.getAdminUsers.mockResolvedValue({ users: rows, orphans: [], email_enabled: false });
  render(Page);
  expect(await screen.findByRole('button', { name: 'Add user' })).toBeDisabled();
  await fireEvent.click(screen.getByRole('button', { name: 'Actions for Legacy' }));
  expect(await screen.findByRole('menuitem', { name: 'Attach email' })).toHaveAttribute(
    'data-disabled',
  );
});
