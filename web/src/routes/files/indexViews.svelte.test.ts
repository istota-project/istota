import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { __history } from '../../../vitest-stubs/app-navigation';
import type { WorkspacePlace } from '$lib/api';
import Files from './+page.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
const identity = vi.hoisted(() => ({ user: { username: 'alice', bot_name: 'Helper' } }));
vi.mock('$lib/api', () => api);
vi.mock('$lib/fileViewer/store.svelte', () => ({ viewer: { openFile: vi.fn() } }));
vi.mock('$lib/userContext', () => ({ getCurrentUser: () => identity }));
await fillApiDouble(api);

const reach = { private: 'rw', shared_member: 'rw', restricted: 'none', note: null } as const;
const home: WorkspacePlace = {
  id: 'home',
  kind: 'home',
  label: 'My files',
  browsable: true,
  view_root: '/Users/alice',
  reach: { ...reach },
};
const room: WorkspacePlace = {
  ...home,
  id: 'room:sample',
  kind: 'room',
  label: 'Project room',
  view_root: '/Channels/sample',
};
const group: WorkspacePlace = {
  ...home,
  id: 'group:team',
  kind: 'group',
  label: 'Team',
  view_root: '/Groups/team',
  reach: { ...reach, private: 'none' },
};
const linked: WorkspacePlace = {
  ...home,
  id: 'linked:1',
  kind: 'linked',
  label: 'Shared docs',
  view_root: '/linked',
};
const configFiles = [
  { name: 'USER.md', kind: 'config', exists: true, size: 2048, editable: true },
  { name: 'CRON.md', kind: 'config', exists: false, size: null, editable: true },
  { name: 'skills/notes', kind: 'skill', exists: false, size: null, editable: true },
].map((file) => ({ ...file, view_path: `/Users/alice/Helper/config/${file.name}` }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  __history.reset('/istota/files');
  api.getWorkspacePins.mockResolvedValue({ pins: [] });
  api.getWorkspaceConfigFiles.mockResolvedValue({ files: configFiles });
  api.getWorkspacePlaces.mockResolvedValue({ places: [home, room, group, linked], notes: [] });
  api.getWorkspaceListing.mockImplementation(async (place, path) => ({
    place,
    path,
    entries: [],
    truncated: false,
    skipped_names: 0,
  }));
  api.getWorkspaceConfigFile.mockResolvedValue({ content: '', revision: 'r1', exists: true });
});
afterEach(cleanup);

async function botSection() {
  return screen.findByRole('region', { name: 'Helper' });
}

it('collects the bot folder entries under one section named for the bot', async () => {
  render(Files);
  const section = await botSection();
  const labels = within(section)
    .getAllByRole('link')
    .map((link) => link.querySelector('.ui-list-row-label')?.textContent);
  expect(labels).toEqual([
    'My files',
    'Rooms',
    'Groups',
    'Configuration',
    'Skill instructions',
    'Scripts',
  ]);
  const sidebar = screen.getByRole('complementary');
  expect(within(sidebar).queryByRole('link', { name: 'Project room' })).toBeNull();
  expect(within(sidebar).queryByRole('link', { name: 'Team' })).toBeNull();
  expect(within(sidebar).queryByText('USER.md')).toBeNull();
  expect(within(sidebar).queryByText('notes')).toBeNull();
  expect(within(sidebar).getByRole('button', { name: /Linked folders/ })).toBeTruthy();
});

async function openIndex(name: string) {
  const section = await botSection();
  await fireEvent.click(within(section).getByRole('link', { name }));
  return screen.findByRole('region', { name });
}

it('marks only My files in the bot section and puts the reach on each index', async () => {
  render(Files);
  const section = await botSection();
  await within(section).findByRole('link', { name: 'Configuration' });
  const marked = within(section)
    .getAllByRole('link')
    .filter((link) => link.querySelector('.task-reach'))
    .map((link) => link.querySelector('.ui-list-row-label')?.textContent);
  expect(marked).toEqual(['My files']);
  const myFiles = within(section).getByRole('link', { name: 'My files' });
  expect(myFiles).toHaveAccessibleDescription('Helper can read and change these files');
  const panel = await screen.findByText(/reaches your files/);
  expect(panel.textContent).toBe('How Helper reaches your files');

  const config = await openIndex('Configuration');
  expect(
    within(config).getByText('Helper can read and change these files, in any of your rooms.'),
  ).toBeTruthy();
  const rooms = await openIndex('Rooms');
  expect(
    within(rooms).getByText('Helper can read and change these files, each only in its own room.'),
  ).toBeTruthy();
  // The fixture's group is not reachable from a private room, so its index claims nothing.
  const groups = await openIndex('Groups');
  expect(groups.querySelector('.reach-note')).toBeNull();
});

it('says when a group folder reaches a shared room', async () => {
  const shared = { ...group, reach: { ...reach } };
  api.getWorkspacePlaces.mockResolvedValue({ places: [home, room, shared], notes: [] });
  render(Files);
  const groups = await openIndex('Groups');
  expect(
    within(groups).getByText(
      'Helper can read and change these files, in a shared room only when everyone there is in the group.',
    ),
  ).toBeTruthy();
});

it('claims no reach for an index whose folders differ in access', async () => {
  const other = { ...group, id: 'group:other', label: 'Other', reach: { ...reach } };
  api.getWorkspacePlaces.mockResolvedValue({ places: [home, room, group, other], notes: [] });
  render(Files);
  const groups = await openIndex('Groups');
  await within(groups).findByText('Other');
  expect(groups.querySelector('.reach-note')).toBeNull();
});

it('leaves out the entries with nothing behind them', async () => {
  api.getWorkspacePlaces.mockResolvedValue({ places: [home], notes: [] });
  api.getWorkspaceConfigFiles.mockResolvedValue({
    files: configFiles.filter((file) => file.kind === 'config'),
  });
  render(Files);
  const section = await botSection();
  await waitFor(() =>
    expect(
      within(section)
        .getAllByRole('link')
        .map((link) => link.querySelector('.ui-list-row-label')?.textContent),
    ).toEqual(['My files', 'Configuration', 'Scripts']),
  );
});

it('opens the rooms index in the main area, then a room with a way back', async () => {
  render(Files);
  const section = await botSection();
  await fireEvent.click(within(section).getByRole('link', { name: 'Rooms' }));
  expect(window.location.search).toBe('?view=rooms');
  const index = await screen.findByRole('region', { name: 'Rooms' });
  expect(within(index).getByText('Read-write')).toBeTruthy();
  expect(within(index).queryByText('Team')).toBeNull();
  expect(api.getWorkspaceListing).not.toHaveBeenCalledWith('room:sample', '');

  await fireEvent.click(within(index).getByRole('link', { name: 'Project room' }));
  await waitFor(() => expect(api.getWorkspaceListing).toHaveBeenCalledWith('room:sample', ''));
  const crumbs = screen.getByRole('navigation', { name: 'Folder path' });
  expect(within(crumbs).getByRole('link', { name: 'Project room' })).toHaveAttribute(
    'aria-current',
    'page',
  );
  expect(within(section).getByRole('link', { name: 'Rooms' })).toHaveAttribute(
    'aria-current',
    'location',
  );

  await fireEvent.click(within(crumbs).getByRole('link', { name: 'Rooms' }));
  expect(await screen.findByRole('region', { name: 'Rooms' })).toBeTruthy();
});

it('lists groups in their own index', async () => {
  __history.reset('/istota/files?view=groups');
  render(Files);
  const index = await screen.findByRole('region', { name: 'Groups' });
  expect(within(index).getByRole('link', { name: 'Team' })).toHaveAttribute(
    'href',
    expect.stringMatching(/\/files\?place=group%3Ateam$/),
  );
  expect(within(index).getByText('No access')).toBeTruthy();
  expect(within(index).queryByText('Project room')).toBeNull();
});

it('opens configuration from a deep link with descriptions and the editor', async () => {
  __history.reset('/istota/files?view=config');
  render(Files);
  const index = await screen.findByRole('region', { name: 'Configuration' });
  expect(await within(index).findByText('USER.md')).toBeTruthy();
  expect(within(index).getByText('Your scheduled jobs.')).toBeTruthy();
  expect(within(index).getByText('Not created yet')).toBeTruthy();
  expect(within(index).queryByText('notes')).toBeNull();
  await fireEvent.click(within(index).getByRole('button', { name: 'USER.md' }));
  await waitFor(() => expect(api.getWorkspaceConfigFile).toHaveBeenCalledWith('USER.md'));
});

it('lists skill instructions by skill name', async () => {
  __history.reset('/istota/files?view=skills');
  render(Files);
  const index = await screen.findByRole('region', { name: 'Skill instructions' });
  expect(await within(index).findByRole('button', { name: 'notes' })).toBeTruthy();
  expect(within(index).queryByText('USER.md')).toBeNull();
});

it('keeps a failed configuration load retryable in the main area', async () => {
  api.getWorkspaceConfigFiles.mockRejectedValueOnce(new Error('down'));
  __history.reset('/istota/files?view=config');
  render(Files);
  const index = await screen.findByRole('region', { name: 'Configuration' });
  expect(await within(index).findByText('Could not load your configuration files.')).toBeTruthy();
  await fireEvent.click(within(index).getByRole('button', { name: 'Retry' }));
  expect(await within(index).findByText('USER.md')).toBeTruthy();
});

it('marks a room index entry as the location, not the page, while a room is open', async () => {
  __history.reset('/istota/files?place=room%3Asample');
  render(Files);
  const section = await botSection();
  await waitFor(() =>
    expect(within(section).getByRole('link', { name: 'Rooms' })).toHaveAttribute(
      'aria-current',
      'location',
    ),
  );
  await fireEvent.click(within(section).getByRole('link', { name: 'Rooms' }));
  expect(within(section).getByRole('link', { name: 'Rooms' })).toHaveAttribute(
    'aria-current',
    'page',
  );
});

it('moves between an index and a folder with Back and Forward', async () => {
  __history.reset('/istota/files?view=rooms');
  render(Files);
  const index = await screen.findByRole('region', { name: 'Rooms' });
  await fireEvent.click(within(index).getByRole('link', { name: 'Project room' }));
  await waitFor(() => expect(api.getWorkspaceListing).toHaveBeenCalledWith('room:sample', ''));
  __history.back();
  window.dispatchEvent(new PopStateEvent('popstate'));
  expect(await screen.findByRole('region', { name: 'Rooms' })).toBeTruthy();
  __history.forward();
  window.dispatchEvent(new PopStateEvent('popstate'));
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Rooms' })).toBeNull());
  expect(screen.getByRole('navigation', { name: 'Folder path' })).toHaveTextContent('Project room');
});

it('shows loading, not an empty list, while a configuration retry is in flight', async () => {
  api.getWorkspaceConfigFiles.mockRejectedValueOnce(new Error('down'));
  __history.reset('/istota/files?view=config');
  render(Files);
  const index = await screen.findByRole('region', { name: 'Configuration' });
  await within(index).findByText('Could not load your configuration files.');
  let resolve!: (value: unknown) => void;
  api.getWorkspaceConfigFiles.mockReturnValueOnce(new Promise((done) => (resolve = done)));
  await fireEvent.click(within(index).getByRole('button', { name: 'Retry' }));
  expect(within(index).getByText('Loading…')).toBeTruthy();
  expect(within(index).queryByText('No configuration files are available.')).toBeNull();
  resolve({ files: configFiles });
  expect(await within(index).findByText('USER.md')).toBeTruthy();
});

it('keeps a file dropped on an index view from navigating the tab', async () => {
  __history.reset('/istota/files?view=config');
  render(Files);
  const index = await screen.findByRole('region', { name: 'Configuration' });
  await within(index).findByText('USER.md');
  const notCancelled = await fireEvent.drop(index, {
    dataTransfer: { files: [], types: ['Files'] },
  });
  expect(notCancelled).toBe(false);
});
