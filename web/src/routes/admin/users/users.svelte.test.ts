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
// Just enough of the editor's payload for it to render; its own behaviour is
// adminUserModal.svelte.test.ts's subject.
const editorDetail = (userId: string) => ({
  user_id: userId,
  is_admin: false,
  identity: null,
  profile: {
    display_name: userId[0].toUpperCase() + userId.slice(1),
    timezone: 'UTC',
    email_addresses: [],
    trusted_email_senders: [],
    quiet_email_senders: [],
    outbound_approval: '',
    disabled_skills: [],
    disabled_modules: [],
    default_briefings: true,
    max_foreground_workers: 0,
    max_background_workers: 0,
    sms_phone_number: '',
  },
  channels: { log_channel: '', alerts_channel: '' },
  whatsapp: { number: '', status: 'unbound', identity: null, provider: null, last_seen_at: null },
  managed: [],
  options: {
    modules: [],
    skills: [],
    outbound_approval: ['', 'off', 'untrusted', 'all'],
    outbound_approval_floor: 'untrusted',
    email_enabled: true,
    email_login_enabled: true,
    sms_enabled: false,
    whatsapp_enabled: false,
  },
});
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.getAdminUsers.mockResolvedValue({ users: rows, orphans: [], email_enabled: true });
  api.createAdminUser.mockResolvedValue({ sent: true });
  api.adminUserAction.mockResolvedValue({ sent: true });
  api.getAdminUser.mockImplementation(async (userId: string) => editorDetail(userId));
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

it('shows the three states and offers only the editor on a Nextcloud-only row', async () => {
  render(Page);
  const row = await screen.findByRole('row', { name: /Legacy/ });
  expect(within(row).getByText('Nextcloud only')).toBeTruthy();
  expect(screen.getByText('Email code')).toBeTruthy();
  expect(screen.getByText('Password set')).toBeTruthy();
  await fireEvent.click(within(row).getByRole('button', { name: 'Actions for Legacy' }));
  const items = await screen.findAllByRole('menuitem');
  expect(items.map((item) => item.textContent?.trim())).toEqual(['Edit settings']);
});

it('opens the settings editor from a row click and from the menu', async () => {
  render(Page);
  await fireEvent.click(await screen.findByText('Bob'));
  expect(await screen.findByRole('dialog', { name: 'Settings for Bob' })).toBeTruthy();
  expect(api.getAdminUser).toHaveBeenLastCalledWith('bob');
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

  await fireEvent.click(screen.getByRole('button', { name: 'Actions for Alice' }));
  const items = await screen.findAllByRole('menuitem');
  expect(items[0]).toHaveTextContent('Edit settings');
  await fireEvent.click(items[0]);
  expect(await screen.findByRole('dialog', { name: 'Settings for Alice' })).toBeTruthy();
  expect(api.getAdminUser).toHaveBeenLastCalledWith('alice');
});

it('opens the editor on a user it has just created', async () => {
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Add user' }));
  const dialog = await screen.findByRole('dialog', { name: 'Add user' });
  await fireEvent.input(within(dialog).getByLabelText('User ID'), { target: { value: 'carol' } });
  await fireEvent.input(within(dialog).getByLabelText('Email'), {
    target: { value: 'carol@example.com' },
  });
  await fireEvent.submit(within(dialog).getByRole('form', { name: 'Add user' }));
  await waitFor(() =>
    expect(api.createAdminUser).toHaveBeenCalledWith({
      user_id: 'carol',
      email: 'carol@example.com',
      display_name: '',
    }),
  );
  await waitFor(() => expect(api.getAdminUser).toHaveBeenCalledWith('carol'));
  expect(await screen.findByRole('dialog', { name: 'Settings for Carol' })).toBeTruthy();
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

it('keeps a create error and the entered email in the dialog', async () => {
  api.createAdminUser.mockRejectedValueOnce(new Error('The invitation could not be sent.'));
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Add user' }));
  const dialog = await screen.findByRole('dialog', { name: 'Add user' });
  await fireEvent.input(within(dialog).getByLabelText('User ID'), { target: { value: 'dave' } });
  await fireEvent.input(within(dialog).getByLabelText('Email'), {
    target: { value: 'dave@example.com' },
  });
  await fireEvent.submit(within(dialog).getByRole('form'));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('could not be sent');
  expect(within(dialog).getByLabelText('Email')).toHaveValue('dave@example.com');
  await waitFor(() => expect(api.getAdminUsers).toHaveBeenCalledTimes(2));
  expect(api.getAdminUser).not.toHaveBeenCalled();
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

it('disables creation when email sign-in is off', async () => {
  api.getAdminUsers.mockResolvedValue({ users: rows, orphans: [], email_enabled: false });
  render(Page);
  expect(await screen.findByRole('button', { name: 'Add user' })).toBeDisabled();
});
