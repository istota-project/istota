import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { __history } from '../../../../vitest-stubs/app-navigation';
import Groups from './+page.svelte';
import Layout from '../+layout.svelte';
import { createRawSnippet } from 'svelte';
import { formatDate } from '$lib/dateFormat';

const api = vi.hoisted(() => ({}) as ApiDouble);
const identity = vi.hoisted(() => ({
  user: { username: 'admin', is_admin: true },
  expireSession: vi.fn(),
  reload: vi.fn(),
}));
vi.mock('$lib/api', () => api);
vi.mock('$lib/userContext', () => ({ getCurrentUser: () => identity }));
await fillApiDouble(api);
const group = {
  group_id: 'team',
  display_name: 'Team',
  kind: 'team',
  revision: 3,
  role: null,
  archived_at: null,
  created_by: 'admin',
  created_at: '2026-10-01',
  needs_owner: true,
  capabilities: {
    member: false,
    owner: false,
    manage: true,
    archive: true,
    read_memory: false,
    edit_memory: false,
    leave: false,
  },
  members: [{ user_id: 'alice', display_name: 'Alice', role: 'member' }],
  history: [
    {
      user_id: 'bob',
      display_name: 'Bob',
      role: 'member',
      added_at: '2026-10-02',
      added_by: 'admin',
      ended_at: '2026-10-03',
      ended_by: 'alice',
    },
  ],
  events: [
    {
      id: 1,
      actor_id: 'admin',
      event_type: 'role_changed',
      subject_user_id: 'alice',
      created_at: '2026-10-04',
      details: { old_role: 'owner', new_role: 'member' },
    },
  ],
  disclosure: { memory: 'Groups/team/GROUP.md', rooms: [], briefings: [] },
};
beforeEach(() => {
  vi.resetAllMocks();
  __history.reset('/istota/admin/groups/?group=team');
  api.getAdminGroups.mockResolvedValue({ groups: [group] });
  api.getAdminGroup.mockResolvedValue(group);
  api.getAdminGroupUserOptions.mockResolvedValue({
    users: [
      { user_id: 'alice', display_name: 'Alice' },
      { user_id: 'carol', display_name: 'Carol' },
    ],
  });
  api.updateAdminGroup.mockResolvedValue({ ...group, revision: 4 });
  api.createAdminGroup.mockResolvedValue({ group_id: 'family', storage_available: true });
  api.archiveAdminGroup.mockResolvedValue({ ...group, archived_at: '2026-10-05' });
  api.retryGroupStorage.mockResolvedValue({ group_id: 'team', storage_available: true });
});
afterEach(cleanup);

it('shows owner recovery, membership history and events without loading notebook content', async () => {
  render(Groups);
  await screen.findByRole('heading', { name: 'Team' });
  expect(screen.getByRole('button', { name: 'Team' })).toHaveAttribute('aria-current', 'true');
  expect(screen.getAllByText('Needs owner').length).toBeGreaterThan(0);
  const history = screen.getByRole('table', { name: 'Membership history' });
  expect(within(history).getByText('Bob')).toBeTruthy();
  expect(within(history).getByText(formatDate('2026-10-03'))).toBeTruthy();
  expect(within(history).getByText('by alice')).toBeTruthy();
  expect(screen.getByText('Role changed')).toBeTruthy();
  expect(screen.getByText('Role: owner → member')).toBeTruthy();
  expect(screen.queryByRole('link', { name: 'Open group notebook' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Leave group' })).toBeNull();
  expect(api.getGroup).not.toHaveBeenCalled();
  expect(api.getGroupMemory).not.toHaveBeenCalled();
});
it('creates a group with explicit enabled owners, without automatically adding the admin', async () => {
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Create group' }));
  const dialog = screen.getByRole('dialog', { name: 'Create group' });
  await fireEvent.input(within(dialog).getByRole('textbox', { name: 'Group ID' }), {
    target: { value: 'family' },
  });
  await fireEvent.input(within(dialog).getByRole('textbox', { name: 'Name' }), {
    target: { value: 'Family' },
  });
  const create = within(dialog).getByRole('button', { name: 'Create group' });
  expect(create).toBeDisabled();
  await fireEvent.click(await within(dialog).findByRole('checkbox', { name: 'Alice' }));
  await fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Carol' }));
  await fireEvent.click(create);
  await waitFor(() =>
    expect(api.createAdminGroup).toHaveBeenCalledWith({
      revision: 0,
      group_id: 'family',
      display_name: 'Family',
      kind: 'group',
      owners: ['alice', 'carol'],
    }),
  );
});
it('requires history acknowledgement to recover ownership with a new member through the admin endpoint', async () => {
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Assign owner' }));
  const dialog = screen.getByRole('dialog', { name: 'Add member' });
  await within(dialog).findByText('Groups/team/GROUP.md');
  const add = within(dialog).getByRole('button', { name: 'Add member' });
  expect(add).toBeDisabled();
  await fireEvent.click(within(dialog).getByRole('checkbox'));
  await fireEvent.click(add);
  await waitFor(() =>
    expect(api.updateAdminGroup).toHaveBeenCalledWith('team', {
      revision: 3,
      user_id: 'carol',
      role: 'owner',
      acknowledge_history: true,
    }),
  );
  expect(api.putGroupMember).not.toHaveBeenCalled();
  expect(api.getGroupUserOptions).not.toHaveBeenCalled();
});
it('promotes an existing member and updates metadata through admin authority', async () => {
  render(Groups);
  await fireEvent.keyDown(await screen.findByRole('button', { name: 'Actions for Alice' }), {
    key: 'Enter',
  });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Make owner' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save role' }));
  await waitFor(() =>
    expect(api.updateAdminGroup).toHaveBeenCalledWith('team', {
      revision: 3,
      user_id: 'alice',
      role: 'owner',
    }),
  );
  await fireEvent.click(await screen.findByRole('button', { name: 'Edit group details' }));
  await fireEvent.input(screen.getByRole('textbox', { name: 'Name' }), {
    target: { value: 'Planning' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Save details' }));
  await waitFor(() =>
    expect(api.updateAdminGroup).toHaveBeenLastCalledWith('team', {
      revision: 3,
      display_name: 'Planning',
      kind: 'team',
    }),
  );
  expect(api.updateGroup).not.toHaveBeenCalled();
});
it('removes membership only after confirmation through the admin endpoint', async () => {
  render(Groups);
  await fireEvent.keyDown(await screen.findByRole('button', { name: 'Actions for Alice' }), {
    key: 'Enter',
  });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove member' }));
  expect(api.updateAdminGroup).not.toHaveBeenCalled();
  await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove' }));
  await waitFor(() =>
    expect(api.updateAdminGroup).toHaveBeenCalledWith('team', {
      revision: 3,
      user_id: 'alice',
      role: null,
    }),
  );
  expect(api.deleteGroupMember).not.toHaveBeenCalled();
});
it('confirms archive, preserves a revision conflict and reloads before retrying', async () => {
  api.archiveAdminGroup.mockRejectedValueOnce(
    new api.GroupApiError(409, 'revision_conflict', 'Group changed; reload before retrying'),
  );
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Archive group' }));
  expect(api.archiveAdminGroup).not.toHaveBeenCalled();
  const dialog = screen.getByRole('dialog');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Archive' }));
  await within(dialog).findByText('Group changed; reload before retrying');
  expect(api.archiveAdminGroup).toHaveBeenCalledWith('team', { revision: 3, confirm: true });
  api.getAdminGroup.mockResolvedValue({ ...group, revision: 4 });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Reload group details' }));
  await waitFor(() => expect(api.getAdminGroup).toHaveBeenCalledTimes(2));
  await fireEvent.click(await screen.findByRole('button', { name: 'Archive group' }));
  api.getAdminGroup.mockResolvedValue({
    ...group,
    revision: 5,
    archived_at: '2026-10-05',
    capabilities: { ...group.capabilities, manage: false, archive: false },
  });
  await fireEvent.click(
    within(screen.getByRole('dialog')).getByRole('button', { name: 'Archive' }),
  );
  await waitFor(() =>
    expect(api.archiveAdminGroup).toHaveBeenLastCalledWith('team', { revision: 4, confirm: true }),
  );
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Archive group' })).toBeNull());
  expect(screen.getByRole('table', { name: 'Membership history' })).toBeTruthy();
});
it('retains a created group when storage fails and retries setup without creating it twice', async () => {
  api.createAdminGroup.mockResolvedValue({ group_id: 'team', storage_available: false });
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Create group' }));
  const dialog = screen.getByRole('dialog');
  await fireEvent.input(within(dialog).getByRole('textbox', { name: 'Group ID' }), {
    target: { value: 'team' },
  });
  await fireEvent.input(within(dialog).getByRole('textbox', { name: 'Name' }), {
    target: { value: 'Team' },
  });
  await fireEvent.click(await within(dialog).findByRole('checkbox', { name: 'Alice' }));
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Create group' }));
  await screen.findByText(/Group created, but storage is unavailable/);
  api.retryGroupStorage.mockRejectedValueOnce(
    new api.GroupApiError(503, 'storage_unavailable', 'Storage still unavailable'),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Retry storage setup' }));
  await screen.findByText('Storage still unavailable');
  await fireEvent.click(screen.getByRole('button', { name: 'Retry storage setup' }));
  await screen.findByText('Group storage is ready.');
  expect(api.retryGroupStorage).toHaveBeenCalledWith('team');
  expect(api.createAdminGroup).toHaveBeenCalledTimes(1);
  expect(api.getGroupMemory).not.toHaveBeenCalled();
});
it('shows an admin refusal without group controls', async () => {
  api.getAdminGroups.mockRejectedValue(
    new api.GroupApiError(403, 'group_forbidden', 'Administrator required'),
  );
  render(Groups);
  await screen.findByText('Administrator required');
  expect(screen.queryByRole('button', { name: 'Create group' })).toBeNull();
});

it('places Groups immediately below Users in admin navigation', () => {
  __history.reset('/admin/groups/');
  render(Layout, { children: createRawSnippet(() => ({ render: () => '<div></div>' })) });
  const links = screen.getAllByRole('link');
  const names = links.map((link) => link.textContent?.trim());
  expect(names[names.indexOf('Users') + 1]).toBe('Groups');
  expect(screen.getByRole('link', { name: 'Groups' })).toHaveAttribute('aria-current', 'page');
});
it('keeps a conflicting membership draft and adopts the refreshed revision', async () => {
  api.updateAdminGroup.mockRejectedValueOnce(
    new api.GroupApiError(409, 'revision_conflict', 'Group changed'),
  );
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Assign owner' }));
  const dialog = screen.getByRole('dialog');
  await fireEvent.click(await within(dialog).findByRole('checkbox'));
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Add member' }));
  await within(dialog).findByText('Group changed');
  api.getAdminGroup.mockResolvedValue({ ...group, revision: 4 });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Reload group details' }));
  await waitFor(() => expect(api.getAdminGroup).toHaveBeenCalledTimes(2));
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Add member' }));
  await waitFor(() =>
    expect(api.updateAdminGroup).toHaveBeenLastCalledWith('team', {
      revision: 4,
      user_id: 'carol',
      role: 'owner',
      acknowledge_history: true,
    }),
  );
});
it('keeps archived groups inspectable without management or notebook controls', async () => {
  api.getAdminGroup.mockResolvedValue({
    ...group,
    archived_at: '2026-10-05',
    capabilities: { ...group.capabilities, manage: false, archive: false },
  });
  render(Groups);
  await screen.findByRole('table', { name: 'Membership history' });
  expect(screen.queryByRole('button', { name: 'Add member' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Archive group' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Retry storage setup' })).toBeNull();
  expect(api.getGroupMemory).not.toHaveBeenCalled();
});

it('keeps automatic groups ownerless without management controls', async () => {
  const automatic = {
    ...group,
    system_key: 'all_users',
    display_name: 'All users',
    needs_owner: false,
    capabilities: { ...group.capabilities, manage: false, archive: false },
  };
  api.getAdminGroups.mockResolvedValue({ groups: [automatic] });
  api.getAdminGroup.mockResolvedValue(automatic);
  render(Groups);
  await screen.findByText('Everyone in this installation; membership is automatic.');
  expect(screen.queryByText('Needs owner')).toBeNull();
  for (const name of [
    'Add member',
    'Assign owner',
    'Edit group details',
    'Archive group',
    'Actions for Alice',
  ])
    expect(screen.queryByRole('button', { name })).toBeNull();
  expect(screen.getByText(/current and future installation users/)).toBeTruthy();
});
