import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { __history } from '../../../vitest-stubs/app-navigation';
import { viewer } from '$lib/fileViewer/store.svelte';
import Groups from './+page.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
const identity = vi.hoisted(() => ({
  user: { username: 'alice', is_admin: false, features: { workspace: true } },
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
  role: 'member',
  capabilities: {
    member: true,
    owner: false,
    manage: false,
    read_memory: true,
    edit_memory: true,
    leave: true,
  },
  members: [
    { user_id: 'alice', display_name: 'Alice', role: 'member' },
    { user_id: 'bob', display_name: 'Bob', role: 'owner' },
  ],
  rooms: [{ token: 'room1', name: 'Planning', origin: 'web' }],
  disclosure: { memory: 'Groups/team/GROUP.md', rooms: [], briefings: [] },
};
const notebook = {
  group_id: 'team',
  status: 'readable',
  content: 'Shared plans',
  revision: 'r1',
  bytes: 12,
  max_bytes: 16777216,
  large: false,
  last_change: null,
};
beforeEach(() => {
  vi.clearAllMocks();
  identity.user.is_admin = false;
  __history.reset('/istota/groups/?group=team');
  api.getGroups.mockResolvedValue({ groups: [group] });
  api.getGroup.mockResolvedValue(group);
  api.getGroupMemory.mockResolvedValue(notebook);
  api.saveGroupMemory.mockResolvedValue({ status: 'ok', revision: 'r2', audit_recorded: true });
  api.getGroupMemoryActivity.mockResolvedValue({ entries: [], next_cursor: null });
  api.getGroupUserOptions.mockResolvedValue({
    users: [{ user_id: 'carol', display_name: 'Carol' }],
  });
  api.getChatRooms.mockResolvedValue({ rooms: [] });
  api.getWorkspaceListing.mockResolvedValue({
    place: 'group:team',
    path: '',
    entries: [],
    truncated: false,
    skipped_names: 0,
  });
  api.getWorkspacePlaces.mockResolvedValue({ places: [], notes: [] });
  identity.user.features.workspace = true;
  viewer.close();
});
afterEach(cleanup);
async function edit() {
  await fireEvent.click(await screen.findByRole('button', { name: 'Edit notebook' }));
  const box = await screen.findByRole('textbox', { name: 'Group notebook' });
  await fireEvent.input(box, { target: { value: 'Updated plans' } });
  return box;
}
it('opens the notebook on the overview and saves through the shared editor with the audience charter', async () => {
  render(Groups);
  await screen.findByText('Shared plans');
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Groups');
  expect(screen.queryByRole('heading', { name: 'Team' })).toBeNull();
  expect(screen.getByText(/current and future group members/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Source' })).toBeNull();
  expect(screen.queryByRole('link', { name: 'Briefings' })).toBeNull();
  expect(await screen.findByRole('link', { name: 'Open in Files' })).toHaveAttribute(
    'href',
    '/files?place=group%3Ateam',
  );
  await edit();
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() =>
    expect(api.saveGroupMemory).toHaveBeenCalledWith('team', 'Updated plans', 'r1'),
  );
});
it('preserves a conflicting draft and offers the shared reload action', async () => {
  api.saveGroupMemory.mockRejectedValue(new api.TextFileConflictError());
  render(Groups);
  const box = await edit();
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByRole('button', { name: 'Discard mine and reload' });
  expect(box).toHaveValue('Updated plans');
});
it.each([
  ['memory_unreadable', 503],
  ['memory_unreadable', 422],
  ['group_not_found', 404],
])('blocks another save after %s (%s) with an open editor', async (code, status) => {
  api.saveGroupMemory.mockRejectedValue(new api.GroupApiError(status, code, 'Access unavailable'));
  render(Groups);
  await edit();
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await within(screen.getByRole('dialog')).findByText('Access unavailable');
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  expect(api.saveGroupMemory).toHaveBeenCalledTimes(1);
});
it.each([503, 422, 413])(
  'never opens an empty editable notebook after a refused GET (%s)',
  async (status) => {
    api.getGroupMemory.mockRejectedValue(
      new api.GroupApiError(status, 'memory_unreadable', 'Cannot read notebook'),
    );
    render(Groups);
    await screen.findByText('Cannot read notebook');
    expect(screen.queryByRole('button', { name: 'Edit notebook' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Group notebook' })).toBeNull();
  },
);
it('allows creating a confirmed missing notebook and reports a failed audit as a successful save', async () => {
  api.getGroupMemory.mockResolvedValue({ ...notebook, status: 'missing', content: '' });
  api.saveGroupMemory.mockResolvedValue({ status: 'ok', revision: 'r2', audit_recorded: false });
  render(Groups);
  await edit();
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await within(screen.getByRole('dialog')).findByText(
    /Saved, but this change could not be recorded/,
  );
});
it('shows every card on one overview, from a pre-overview view link too', async () => {
  __history.reset('/istota/groups/?group=team&view=rooms');
  render(Groups);
  expect(await screen.findByRole('link', { name: 'Planning' })).toHaveAttribute(
    'href',
    '/chat?room=room1',
  );
  for (const name of ['Notebook', 'Members', 'Rooms', 'Shared files'])
    expect(screen.getByRole('heading', { name })).toBeTruthy();
  expect(screen.queryByRole('navigation', { name: 'Group view' })).toBeNull();
  await screen.findByText('Bob');
  for (const role of ['Owner', 'Member'])
    expect(screen.getByText(role).classList.contains('badge')).toBe(true);
  expect(screen.queryByRole('button', { name: 'Add member' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Leave group' })).toBeTruthy();
});
it('orders linked rooms by latest activity and shows unread counts', async () => {
  api.getGroup.mockResolvedValue({
    ...group,
    rooms: [
      { token: 'old', name: 'Quiet room', origin: 'web' },
      { token: 'new', name: 'Busy room', origin: 'web' },
      { token: 'gone', name: 'Unlisted room', origin: 'talk' },
    ],
  });
  api.getChatRooms.mockResolvedValue({
    rooms: [
      { token: 'old', last_activity: '2026-10-01T10:00:00Z', unread_count: 0 },
      { token: 'new', last_activity: '2026-10-13T10:00:00Z', unread_count: 3 },
    ],
  });
  render(Groups);
  const card = (await screen.findByRole('heading', { name: 'Rooms' })).closest('section')!;
  await within(card).findByText('3');
  expect(
    within(card)
      .getAllByRole('link')
      .map((link) => link.textContent),
  ).toEqual(['Busy room', 'Quiet room', 'Unlisted room']);
});
it('lists the newest shared files and opens one in the viewer', async () => {
  api.getWorkspacePlaces.mockResolvedValue({
    places: [{ id: 'group:team', view_root: 'Groups/team' }],
    notes: [],
  });
  api.getWorkspaceListing.mockResolvedValue({
    place: 'group:team',
    path: '',
    entries: [
      { name: 'old.md', kind: 'file', size: 1, modified: 1000, hidden: false },
      { name: 'plans', kind: 'dir', size: null, modified: 3000, hidden: false },
      { name: 'new.md', kind: 'file', size: 1, modified: 2000, hidden: false },
      { name: '.secret', kind: 'file', size: 1, modified: 4000, hidden: true },
    ],
    truncated: false,
    skipped_names: 0,
  });
  render(Groups);
  const card = (await screen.findByRole('heading', { name: 'Shared files' })).closest('section')!;
  await within(card).findByText('new.md');
  expect(within(card).queryByText('.secret')).toBeNull();
  expect(within(card).getByRole('link', { name: 'plans' })).toHaveAttribute(
    'href',
    '/files?place=group%3Ateam&path=plans',
  );
  const names = [...card.querySelectorAll('.name')].map((el) => el.textContent);
  expect(names).toEqual(['plans', 'new.md', 'old.md']);
  await fireEvent.click(within(card).getByRole('button', { name: 'new.md' }));
  expect(viewer.state).toEqual({
    mode: 'file',
    path: 'Groups/team/new.md',
    source: { kind: 'workspace' },
  });
  expect(api.getWorkspaceListing).toHaveBeenCalledWith('group:team');
});
it('leaves the files card out without the workspace feature', async () => {
  identity.user.features.workspace = false;
  render(Groups);
  await screen.findByText('Shared plans');
  expect(screen.queryByRole('heading', { name: 'Shared files' })).toBeNull();
  expect(api.getWorkspaceListing).not.toHaveBeenCalled();
});
it('paginates recorded outcomes without claiming a complete history', async () => {
  api.getGroupMemoryActivity
    .mockResolvedValueOnce({
      entries: [
        {
          id: 'new',
          user_id: 'bob',
          ts: '2026-10-10T10:00:00Z',
          source: 'web',
          applied: [{ op: { op: 'edit' }, outcome: 'applied' }],
          rejected: [],
        },
      ],
      next_cursor: 'older',
    })
    .mockResolvedValueOnce({
      entries: [
        {
          id: 'old',
          user_id: 'alice',
          ts: '2026-10-09T10:00:00Z',
          applied: [{ op: { op: 'append' }, outcome: 'applied' }],
          rejected: [],
        },
      ],
      next_cursor: null,
    });
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Recorded changes' }));
  await screen.findByText('edit: applied');
  await fireEvent.click(screen.getByRole('button', { name: 'Load older changes' }));
  await screen.findByText('append: applied');
  expect(api.getGroupMemoryActivity).toHaveBeenLastCalledWith('team', 'older');
  expect(screen.getByText(/Direct file edits may not appear/)).toBeTruthy();
});
it.each([false, true])('renders a private empty state (admin=%s)', async (admin) => {
  identity.user.is_admin = admin;
  api.getGroups.mockResolvedValue({ groups: [] });
  render(Groups);
  await screen.findByText('You do not belong to any groups.');
  expect(Boolean(screen.queryByRole('link', { name: 'Manage groups' }))).toBe(admin);
  expect(api.getGroup).not.toHaveBeenCalled();
});
it('requires history acknowledgement before an owner adds a member', async () => {
  __history.reset('/istota/groups/?group=team');
  api.getGroup.mockResolvedValue({
    ...group,
    role: 'owner',
    capabilities: { ...group.capabilities, manage: true, owner: true },
  });
  api.putGroupMember.mockResolvedValue({ ...group, revision: 4 });
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Add member' }));
  const dialog = await screen.findByRole('dialog', { name: 'Add member' });
  expect(within(dialog).getByText('Groups/team/GROUP.md')).toBeTruthy();
  const submit = within(dialog).getByRole('button', { name: 'Add member' });
  expect(submit).toBeDisabled();
  await fireEvent.click(within(dialog).getByRole('checkbox'));
  await fireEvent.click(submit);
  await waitFor(() =>
    expect(api.putGroupMember).toHaveBeenCalledWith('team', 'carol', {
      revision: 3,
      role: 'member',
      acknowledge_history: true,
    }),
  );
});

it('lets a member leave with confirmation and refreshes navigation', async () => {
  __history.reset('/istota/groups/?group=team');
  api.deleteGroupMember.mockResolvedValue({ ...group, role: null });
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Leave group' }));
  expect(api.deleteGroupMember).not.toHaveBeenCalled();
  api.getGroups.mockResolvedValue({ groups: [] });
  await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Leave' }));
  await screen.findByText('You do not belong to any groups.');
  expect(api.deleteGroupMember).toHaveBeenCalledWith('team', 'alice', { revision: 3 });
  expect(identity.reload).toHaveBeenCalled();
});
it('edits owner metadata with the current revision', async () => {
  __history.reset('/istota/groups/?group=team');
  api.getGroup.mockResolvedValue({
    ...group,
    capabilities: { ...group.capabilities, manage: true },
  });
  api.updateGroup.mockResolvedValue({ ...group, revision: 4 });
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Edit group details' }));
  await fireEvent.input(screen.getByRole('textbox', { name: 'Name' }), {
    target: { value: 'Planning team' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Save details' }));
  await waitFor(() =>
    expect(api.updateGroup).toHaveBeenCalledWith('team', {
      revision: 3,
      display_name: 'Planning team',
      kind: 'team',
    }),
  );
});
it('offers replacement ownership and reports the backend last-owner refusal in place', async () => {
  __history.reset('/istota/groups/?group=team');
  api.getGroup.mockResolvedValue({
    ...group,
    capabilities: { ...group.capabilities, manage: true },
  });
  api.deleteGroupMember.mockRejectedValue(
    new api.GroupApiError(409, 'last_owner', 'Choose a replacement owner'),
  );
  render(Groups);
  await fireEvent.keyDown(await screen.findByRole('button', { name: 'Actions for Bob' }), {
    key: 'Enter',
  });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove member' }));
  const dialog = await screen.findByRole('dialog', { name: 'Remove member' });
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await within(dialog).findByText('Choose a replacement owner');
  api.deleteGroupMember.mockResolvedValue({ ...group, revision: 4 });
  const select = within(dialog).getByRole('button', { name: 'Replacement owner' });
  await fireEvent.pointerDown(select, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(select, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(select);
  const option = await screen.findByRole('option', { name: 'Alice' });
  await fireEvent.pointerDown(option, { pointerType: 'mouse' });
  await fireEvent.pointerUp(option, { pointerType: 'mouse' });
  await fireEvent.click(option);
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await waitFor(() =>
    expect(api.deleteGroupMember).toHaveBeenLastCalledWith('team', 'bob', {
      revision: 3,
      replacement_owner: 'alice',
    }),
  );
});
it('shows the large warning and lets only an admin retry storage', async () => {
  api.getGroupMemory.mockResolvedValue({ ...notebook, large: true });
  const first = render(Groups);
  await screen.findByText('Large notebook');
  first.unmount();
  identity.user.is_admin = true;
  api.getGroupMemory.mockRejectedValue(
    new api.GroupApiError(
      503,
      'memory_unreadable',
      'Group memory cannot be read',
      'storage_unavailable',
    ),
  );
  api.retryGroupStorage.mockResolvedValue({ storage_available: true });
  render(Groups);
  await screen.findByText(
    'Group storage is unavailable. Notebook editing is disabled until storage is restored.',
  );
  api.getGroupMemory.mockResolvedValue(notebook);
  await fireEvent.click(screen.getByRole('button', { name: 'Retry storage setup' }));
  await screen.findByText('Shared plans');
  expect(api.retryGroupStorage).toHaveBeenCalledWith('team');
});

it('selects a remaining group after leaving the group named in the URL', async () => {
  __history.reset('/istota/groups/?group=team');
  const other = { ...group, group_id: 'other', display_name: 'Other team' };
  api.deleteGroupMember.mockResolvedValue({ ...group, role: null });
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Leave group' }));
  api.getGroups.mockResolvedValue({ groups: [other] });
  api.getGroup.mockImplementation(async (id) => {
    if (id === 'team') throw new api.GroupApiError(404, 'group_not_found', 'Group not found');
    return other;
  });
  await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Leave' }));
  await waitFor(() =>
    expect(screen.getByRole('link', { name: 'Other team' })).toHaveClass('active'),
  );
  expect(api.getGroup).toHaveBeenLastCalledWith('other');
});
it('refuses an editor reload after the notebook became unreadable', async () => {
  render(Groups);
  await screen.findByText('Shared plans');
  api.getGroupMemory.mockRejectedValue(
    new api.GroupApiError(422, 'memory_unreadable', 'Notebook cannot be read'),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Edit notebook' }));
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByText('Notebook cannot be read');
  expect(within(dialog).queryByRole('textbox')).toBeNull();
  expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled();
});

it('keeps an owner metadata draft when reloading a revision conflict', async () => {
  __history.reset('/istota/groups/?group=team');
  const owned = { ...group, capabilities: { ...group.capabilities, manage: true } };
  api.getGroup.mockResolvedValue(owned);
  api.updateGroup.mockRejectedValueOnce(
    new api.GroupApiError(409, 'revision_conflict', 'Group changed; reload before saving'),
  );
  render(Groups);
  await fireEvent.click(await screen.findByRole('button', { name: 'Edit group details' }));
  await fireEvent.input(screen.getByRole('textbox', { name: 'Name' }), {
    target: { value: 'My draft name' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Save details' }));
  await screen.findByText('Group changed; reload before saving');
  const edited = { ...owned, revision: 4, display_name: 'Someone else edited' };
  api.getGroup.mockResolvedValue(edited);
  api.getGroups.mockResolvedValue({ groups: [edited] });
  await fireEvent.click(screen.getByRole('button', { name: 'Reload group details' }));
  await screen.findByRole('link', { name: 'Someone else edited' });
  expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('My draft name');
  api.updateGroup.mockResolvedValue({ ...owned, revision: 5 });
  await fireEvent.click(screen.getByRole('button', { name: 'Save details' }));
  await waitFor(() =>
    expect(api.updateGroup).toHaveBeenLastCalledWith('team', {
      revision: 4,
      display_name: 'My draft name',
      kind: 'team',
    }),
  );
});

it('explains automatic membership and future history beside shared content', async () => {
  api.getGroup.mockResolvedValue({
    ...group,
    system_key: 'all_users',
    display_name: 'All users',
    capabilities: { ...group.capabilities, manage: false, leave: false },
  });
  render(Groups);
  await screen.findByText('Everyone in this installation; membership is automatic.');
  expect(screen.getByText(/current and future installation users/)).toBeTruthy();
  for (const name of ['Add member', 'Leave group', 'Edit group details', 'Actions for Bob'])
    expect(screen.queryByRole('button', { name })).toBeNull();
  expect(screen.getByRole('heading', { name: 'Notebook' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Shared files' })).toBeTruthy();
});
