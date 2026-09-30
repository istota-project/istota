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
  const row = await screen.findByRole('region', { name: 'Legacy.User' });
  expect(within(row).getByText('Nextcloud only')).toBeTruthy();
  expect(within(row).queryByRole('button', { name: 'Disable' })).toBeNull();
  expect(within(row).queryByRole('button', { name: 'Sign out everywhere' })).toBeNull();
  expect(screen.getByText('No password; sign-in links available')).toBeTruthy();
  expect(screen.getByText('Password set')).toBeTruthy();
  await fireEvent.click(within(row).getByRole('button', { name: 'Attach email' }));
  await fireEvent.input(screen.getByLabelText('Email'), {
    target: { value: 'legacy@example.com' },
  });
  await fireEvent.submit(screen.getByRole('form', { name: 'Attach email' }));
  await waitFor(() =>
    expect(api.createAdminUser).toHaveBeenCalledWith({
      user_id: 'Legacy.User',
      email: 'legacy@example.com',
      display_name: '',
    }),
  );
});

it('names disable and removal effects and sends links through the API', async () => {
  render(Page);
  const row = await screen.findByRole('region', { name: 'bob' });
  expect(screen.getByText(/Disable blocks email and Nextcloud/)).toBeTruthy();
  expect(screen.getByText(/Remove.*fresh Nextcloud sign-in/)).toBeTruthy();
  expect(screen.getByText(/profile and user data/)).toBeTruthy();
  api.adminUserAction.mockRejectedValueOnce(new Error('The sign-in link could not be sent.'));
  await fireEvent.click(within(row).getByRole('button', { name: 'Send sign-in link' }));
  await waitFor(() => expect(api.adminUserAction).toHaveBeenCalledWith('bob', 'login-link'));
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be sent');
});
