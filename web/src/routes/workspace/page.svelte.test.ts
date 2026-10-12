import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { __history } from '../../../vitest-stubs/app-navigation';
import type { WorkspacePlace } from '$lib/api';
import Workspace from './+page.svelte';
import workspaceSource from './+page.svelte?raw';

const api = vi.hoisted(() => ({}) as ApiDouble);
const openFile = vi.hoisted(() => vi.fn());
vi.mock('$lib/api', () => api);
vi.mock('$lib/fileViewer/store.svelte', () => ({ viewer: { openFile } }));
await fillApiDouble(api);
const actualApi = await vi.importActual<typeof import('$lib/api')>('$lib/api');
api.chatFileUrl.mockImplementation(actualApi.chatFileUrl);

const home: WorkspacePlace = {
  id: 'home',
  kind: 'home',
  label: 'My files',
  browsable: true,
  view_root: '/Users/alice',
  reach: {
    private: 'rw',
    shared_member: 'rw',
    restricted: 'none',
    note: 'Personal memory is not loaded into the prompt in shared rooms.',
  },
};
const room: WorkspacePlace = {
  ...home,
  id: 'room:sample',
  kind: 'room',
  label: 'Project room',
  view_root: '/Channels/sample',
};
const entry = (name: string, kind = 'file', hidden = false) => ({
  name,
  kind,
  hidden,
  size: kind === 'file' ? 1024 : null,
  modified: 1767268800,
});

beforeEach(() => {
  vi.clearAllMocks();
  api.getWorkspacePins = vi.fn().mockResolvedValue({ pins: [] });
  api.putWorkspacePin = vi.fn();
  api.deleteWorkspacePin = vi.fn().mockResolvedValue({ status: 'ok' });
  localStorage.clear();
  __history.reset('/istota/workspace');
  api.getWorkspacePlaces.mockResolvedValue({
    places: [home, room],
    notes: ['Files are stored in a local folder.'],
  });
  api.getWorkspaceListing.mockImplementation(async (place, path) => ({
    place,
    path,
    entries: path
      ? [entry('plan #1.md')]
      : [
          entry('notes & drafts', 'dir'),
          entry('.config', 'file', true),
          entry('outside', 'link'),
          entry('socket', 'other'),
        ],
    truncated: false,
    skipped_names: 0,
  }));
});
afterEach(cleanup);

async function openActions(name: string) {
  const trigger = await screen.findByRole('button', { name: `Actions for ${name}` });
  await fireEvent.keyDown(trigger, { key: 'Enter' });
}

it('reserves the folder button height while the listing loads', async () => {
  let resolve!: (value: unknown) => void;
  api.getWorkspaceListing.mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const { container } = render(Workspace);
  await screen.findByRole('checkbox', { name: 'Show hidden' });
  const actions = container.querySelector('.folder-actions');
  expect(actions).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'New folder' })).toBeNull();
  // jsdom has no layout; pin the reserved height to the buttons' shared size.
  expect(workspaceSource).toMatch(
    /\.folder-actions\s*\{[^}]*min-height:\s*var\(--control-height-sm\)/,
  );
  resolve({ place: 'home', path: '', entries: [], truncated: false, skipped_names: 0 });
  await screen.findByRole('button', { name: 'New folder' });
  expect(screen.getByRole('button', { name: 'Upload' })).toBeEnabled();
  expect(container.querySelector('.folder-actions')).toBe(actions);
});

it('keeps the standard sidebar uncluttered and shows access beside the folder path', async () => {
  const { container } = render(Workspace);
  await screen.findByRole('link', { name: 'Project room' });
  const sidebar = screen.getByRole('complementary');
  expect(sidebar.style.getPropertyValue('--sidebar-width')).toBe('220px');
  expect(sidebar.querySelector('.badge')).toBeNull();
  expect(container.querySelector('.folder-toolbar')).toHaveTextContent('Read-write');
  await screen.findByRole('link', { name: 'notes & drafts' });
  expect(container.querySelector('details')).not.toHaveAttribute('open');
  expect(screen.getByText('Files are stored in a local folder.')).toBeTruthy();
  expect(screen.getByText('Guest turn or unasked task')).toBeTruthy();
  expect(screen.getByText('No access')).toBeTruthy();
});

it('keeps untrusted room labels inert in the sidebar and breadcrumbs', async () => {
  api.getWorkspacePlaces.mockResolvedValue({
    places: [{ ...room, label: '<b>Project room</b>' }],
    notes: [],
  });
  const { container } = render(Workspace);
  const sidebar = await screen.findByRole('complementary');
  await within(sidebar).findByRole('link', { name: '<b>Project room</b>' });
  expect(container.querySelector('b')).toBeNull();
  const breadcrumbs = screen.getByRole('navigation', { name: 'Folder path' });
  expect(within(breadcrumbs).getByRole('link', { name: '<b>Project room</b>' })).toBeTruthy();
});

it('navigates encoded folders and breadcrumbs through history, and opens home files in the viewer', async () => {
  render(Workspace);
  await fireEvent.click(await screen.findByRole('link', { name: 'notes & drafts' }));
  await fireEvent.click(await screen.findByRole('button', { name: 'plan #1.md' }));
  expect(openFile).toHaveBeenCalledWith('/Users/alice/notes & drafts/plan #1.md');
  expect(new URL(location.href).searchParams.get('path')).toBe('notes & drafts');
  const breadcrumbs = screen.getByRole('navigation', { name: 'Folder path' });
  await fireEvent.click(within(breadcrumbs).getByRole('link', { name: 'My files' }));
  await screen.findByRole('link', { name: 'notes & drafts' });
  __history.back();
  await screen.findByRole('button', { name: 'plan #1.md' });
  __history.forward();
  await screen.findByRole('link', { name: 'notes & drafts' });
});

it('loads the folder from a direct URL and keeps symbolic links and other entries inert', async () => {
  __history.reset('/istota/workspace?place=home&path=notes%20%26%20drafts');
  const first = render(Workspace);
  await screen.findByRole('button', { name: 'plan #1.md' });
  expect(api.getWorkspaceListing).toHaveBeenCalledWith('home', 'notes & drafts');
  first.unmount();
  __history.reset('/istota/workspace');
  render(Workspace);
  const link = await screen.findByText('outside');
  expect(link.closest('a,button')).toBeNull();
  await fireEvent.click(link);
  expect(openFile).not.toHaveBeenCalled();
  expect(screen.getByText('socket').closest('a,button')).toBeNull();
});

it('persists hidden entries and explains partial listings', async () => {
  api.getWorkspaceListing.mockResolvedValue({
    place: 'home',
    path: '',
    entries: [entry('.config', 'file', true)],
    truncated: true,
    skipped_names: 2,
  });
  const first = render(Workspace);
  await screen.findByText('No visible files in this folder.');
  expect(screen.queryByRole('button', { name: '.config' })).toBeNull();
  await fireEvent.click(screen.getByRole('checkbox', { name: 'Show hidden' }));
  await screen.findByRole('button', { name: '.config' });
  expect(screen.getByText('Showing the first 2,000 entries.')).toBeTruthy();
  expect(screen.getByText("2 entries with names that can't be shown.")).toBeTruthy();
  first.unmount();
  render(Workspace);
  expect(await screen.findByRole('button', { name: '.config' })).toBeTruthy();
});

it('keeps files without a viewer root inert', async () => {
  api.getWorkspacePlaces.mockResolvedValue({ places: [{ ...room, view_root: null }], notes: [] });
  __history.reset('/istota/workspace?place=room%3Asample&path=notes');
  render(Workspace);
  expect((await screen.findByText('plan #1.md')).closest('a,button')).toBeNull();
});

it('shows an unavailable place without falling back to home', async () => {
  __history.reset('/istota/workspace?place=room%3Aremoved');
  render(Workspace);
  await screen.findByText('This place is no longer available.');
  expect(api.getWorkspaceListing).not.toHaveBeenCalled();
});

it('keeps a failed folder retryable and ignores a late result after navigation', async () => {
  let resolve!: (value: unknown) => void;
  api.getWorkspaceListing.mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  render(Workspace);
  await fireEvent.click(await screen.findByRole('link', { name: 'Project room' }));
  await screen.findByRole('link', { name: 'notes & drafts' });
  resolve({
    place: 'home',
    path: '',
    entries: [entry('stale.txt')],
    truncated: false,
    skipped_names: 0,
  });
  await waitFor(() => expect(screen.queryByText('stale.txt')).toBeNull());
  api.getWorkspaceListing.mockRejectedValueOnce(new Error('Folder not found.'));
  await fireEvent.click(screen.getByRole('link', { name: 'notes & drafts' }));
  await screen.findByText('Folder not found.');
  await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  await screen.findByText('plan #1.md');
});

it('shows the empty state when there are no places', async () => {
  api.getWorkspacePlaces.mockResolvedValue({ places: [], notes: [] });
  render(Workspace);
  await screen.findByText('No workspace files or connections are available.');
});

it('recovers from a failed places request', async () => {
  api.getWorkspacePlaces.mockRejectedValueOnce(new Error('Unavailable'));
  render(Workspace);
  await screen.findByText('Could not load your workspace.');
  await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  await screen.findByRole('link', { name: 'notes & drafts' });
});

it('shows connection reach without requesting a directory listing', async () => {
  api.getWorkspacePlaces.mockResolvedValue({
    places: [
      {
        ...home,
        id: 'drive',
        kind: 'drive',
        label: 'Google Drive',
        browsable: false,
        view_root: null,
        reach: { private: 'skill', shared_member: 'skill', restricted: 'none', note: null },
      },
    ],
    notes: [],
  });
  render(Workspace);
  await screen.findByText('This connection is reached through a skill during a task.');
  expect(api.getWorkspaceListing).not.toHaveBeenCalled();
  expect(screen.queryByRole('checkbox', { name: 'Show hidden' })).toBeNull();
});

it.each([
  ['room', '/Channels/sample'],
  ['group', '/Groups/team'],
  ['linked', '/linked/notes'],
] as const)('opens %s files in the shared viewer', async (kind, view_root) => {
  api.getWorkspacePlaces.mockResolvedValue({
    places: [{ ...home, id: kind, kind, view_root }],
    notes: [],
  });
  __history.reset(`/istota/workspace?place=${kind}&path=notes`);
  render(Workspace);
  await fireEvent.click(await screen.findByRole('button', { name: 'plan #1.md' }));
  expect(openFile).toHaveBeenCalledWith(`${view_root}/notes/plan #1.md`);
});

it('creates a folder in the open home directory and refreshes it', async () => {
  api.createWorkspaceFolder.mockResolvedValue({ path: 'notes & drafts/New folder' });
  render(Workspace);
  await fireEvent.click(await screen.findByRole('link', { name: 'notes & drafts' }));
  await fireEvent.click(screen.getByRole('button', { name: 'New folder' }));
  await fireEvent.input(screen.getByRole('textbox', { name: 'Folder name' }), {
    target: { value: 'New folder' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Create folder' }));
  await waitFor(() =>
    expect(api.createWorkspaceFolder).toHaveBeenCalledWith('notes & drafts', 'New folder'),
  );
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(api.getWorkspaceListing).toHaveBeenLastCalledWith('home', 'notes & drafts');
});

it('uploads sequentially to the captured folder and keeps server errors on each row', async () => {
  let finish!: (value: unknown) => void;
  api.uploadWorkspaceFile.mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  api.uploadWorkspaceFile.mockRejectedValueOnce(new Error('file exceeds 100 MB'));
  const { container } = render(Workspace);
  await screen.findByRole('button', { name: 'Upload' });
  const files = [new File(['one'], 'one.txt'), new File(['two'], 'two.txt')];
  await fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files } });
  await waitFor(() => expect(api.uploadWorkspaceFile).toHaveBeenCalledTimes(1));
  await fireEvent.click(screen.getByRole('link', { name: 'notes & drafts' }));
  finish({ name: 'one.txt', path: 'one.txt', size: 3 });
  await screen.findByText('file exceeds 100 MB');
  expect(api.uploadWorkspaceFile.mock.calls.map((args) => args[0])).toEqual(['', '']);
  expect(api.uploadWorkspaceFile.mock.calls.map((args) => args[1])).toEqual(files);
});

it('shows folder conflicts in the dialog and hides writes for other places', async () => {
  api.createWorkspaceFolder.mockRejectedValue(new Error('A file or folder already exists.'));
  render(Workspace);
  await fireEvent.click(await screen.findByRole('button', { name: 'New folder' }));
  await fireEvent.input(screen.getByRole('textbox', { name: 'Folder name' }), {
    target: { value: 'Notes' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Create folder' }));
  await screen.findByText('A file or folder already exists.');
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await fireEvent.click(screen.getByRole('link', { name: 'Project room' }));
  expect(screen.queryByRole('button', { name: 'Upload' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'New folder' })).toBeNull();
});

it('shows share state and a separate Nextcloud action while preserving the viewer', async () => {
  api.getWorkspaceListing.mockResolvedValue({
    place: 'home',
    path: '',
    truncated: false,
    skipped_names: 0,
    entries: [
      {
        ...entry('plan.md'),
        share: { kind: 'user', count: 2 },
        nextcloud_url: 'https://cloud.example.com/apps/files/files?dir=%2FShared',
      },
    ],
    notes: ['Nextcloud share state is unavailable.'],
  });
  render(Workspace);
  await openActions('plan.md');
  const link = await screen.findByRole('menuitem', { name: 'Open in Nextcloud' });
  expect(link).toHaveAttribute('href', 'https://cloud.example.com/apps/files/files?dir=%2FShared');
  expect(link).toHaveAttribute('target', '_blank');
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  expect(screen.getByText('Shared · 2')).toBeTruthy();
  expect(screen.getByText('Nextcloud share state is unavailable.')).toBeTruthy();
  await fireEvent.keyDown(link, { key: 'Escape' });
  await fireEvent.click(screen.getByRole('button', { name: 'plan.md' }));
  expect(openFile).toHaveBeenCalledWith('/Users/alice/plan.md');
});

const drivePlace: WorkspacePlace = {
  ...home,
  id: 'drive',
  kind: 'drive',
  label: 'Google Drive',
  view_root: null,
  reach: { private: 'skill', shared_member: 'skill', restricted: 'none', note: null },
};
const driveItem = (id: string, name: string, is_folder = false) => ({
  id,
  name,
  is_folder,
  mime_type: is_folder ? 'application/vnd.google-apps.folder' : 'application/pdf',
  size: null,
  modified: null,
  web_url: `https://drive.google.com/file/d/${id}/view`,
});
function withDrive() {
  api.getWorkspacePlaces.mockResolvedValue({ places: [home, drivePlace], notes: [] });
  __history.reset('/istota/workspace?place=drive');
}

it('browses Drive roots and folders, preserves readable breadcrumbs, and opens safe links', async () => {
  withDrive();
  api.getWorkspaceDriveListing.mockImplementation(async (folder) => ({
    items:
      folder === 'root'
        ? [driveItem('folder_12345', '<b>Plans / drafts</b>', true)]
        : [driveItem('file_123456', 'Budget.pdf')],
    next_page_token: null,
  }));
  const { container } = render(Workspace);
  const folder = await screen.findByRole('link', { name: '<b>Plans / drafts</b>' });
  expect(folder.querySelector('b')).toBeNull();
  await fireEvent.click(folder);
  const file = await screen.findByRole('link', { name: 'Budget.pdf' });
  expect(file).toHaveAttribute('target', '_blank');
  expect(file).toHaveAttribute('rel', 'noopener noreferrer');
  expect(api.getWorkspaceDriveListing).toHaveBeenLastCalledWith('folder_12345');
  expect(
    within(screen.getByRole('navigation', { name: 'Folder path' })).getByText(
      '<b>Plans / drafts</b>',
    ),
  ).toBeTruthy();
  expect(container.querySelector('time')).toBeNull();
  expect(screen.queryByText('Show hidden')).toBeNull();
  const saved = location.pathname + location.search;
  cleanup();
  __history.reset(saved);
  render(Workspace);
  await screen.findByRole('link', { name: 'Budget.pdf' });
  expect(
    within(screen.getByRole('navigation', { name: 'Folder path' })).getByText(
      '<b>Plans / drafts</b>',
    ),
  ).toBeTruthy();
  __history.reset(screen.getByRole('link', { name: 'Shared with me' }).getAttribute('href')!);
  await waitFor(() => expect(api.getWorkspaceDriveListing).toHaveBeenLastCalledWith('shared'));
  expect(openFile).not.toHaveBeenCalled();
});

it('only pages Drive on demand and discards pages after navigation', async () => {
  withDrive();
  let finish!: (value: unknown) => void;
  api.getWorkspaceDriveListing.mockResolvedValueOnce({
    items: [driveItem('file_123456', 'First.pdf')],
    next_page_token: 'next',
  });
  api.getWorkspaceDriveListing.mockReturnValueOnce(new Promise((resolve) => (finish = resolve)));
  api.getWorkspaceDriveListing.mockResolvedValue({
    items: [driveItem('shared_12345', 'Shared.pdf')],
    next_page_token: null,
  });
  render(Workspace);
  await screen.findByRole('link', { name: 'First.pdf' });
  expect(api.getWorkspaceDriveListing).toHaveBeenCalledTimes(1);
  await fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
  expect(api.getWorkspaceDriveListing).toHaveBeenLastCalledWith('root', 'next');
  __history.reset(screen.getByRole('link', { name: 'Shared with me' }).getAttribute('href')!);
  await screen.findByRole('link', { name: 'Shared.pdf' });
  finish({ items: [driveItem('late_123456', 'Late.pdf')], next_page_token: 'next' });
  await waitFor(() => expect(screen.queryByText('Late.pdf')).toBeNull());
  expect(screen.queryByText('First.pdf')).toBeNull();
});

it('shows the Connections path when Drive access is off', async () => {
  withDrive();
  api.getWorkspaceDriveListing.mockRejectedValue(new Error('drive_not_granted'));
  render(Workspace);
  await screen.findByText('Drive access is off');
  expect(screen.getByRole('link', { name: 'Connections' })).toHaveAttribute(
    'href',
    '/settings/connections',
  );
});

it('retries a failed next page in place and avoids duplicate rows', async () => {
  withDrive();
  api.getWorkspaceDriveListing.mockResolvedValueOnce({
    items: [driveItem('file_123456', 'First.pdf')],
    next_page_token: 'same',
  });
  api.getWorkspaceDriveListing.mockRejectedValueOnce(
    new Error('Could not load Google Drive. Try again.'),
  );
  api.getWorkspaceDriveListing.mockResolvedValueOnce({
    items: [driveItem('file_123456', 'First.pdf'), driveItem('next_123456', 'Next.pdf')],
    next_page_token: 'same',
  });
  render(Workspace);
  await screen.findByRole('link', { name: 'First.pdf' });
  await fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
  await screen.findByRole('alert');
  expect(screen.getByRole('link', { name: 'First.pdf' })).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
  await screen.findByRole('link', { name: 'Next.pdf' });
  expect(screen.getAllByRole('link', { name: 'First.pdf' })).toHaveLength(1);
  expect(api.getWorkspaceDriveListing).toHaveBeenCalledTimes(3);
  expect(api.getWorkspaceDriveListing).toHaveBeenLastCalledWith('root', 'same');
});

it('links to Connections when Google rejects a stored credential', async () => {
  withDrive();
  api.getWorkspaceDriveListing.mockRejectedValue(
    new Error('Google rejected the stored credential. Reconnect in Settings.'),
  );
  render(Workspace);
  await screen.findByText('Google rejected the stored credential. Reconnect in Settings.');
  expect(screen.getByRole('link', { name: 'Connections' })).toHaveAttribute(
    'href',
    '/settings/connections',
  );
});

it('pins a file from its row and removes it from the Pinned section', async () => {
  __history.reset('/istota/workspace?place=home&path=notes');
  api.putWorkspacePin.mockImplementation(async (body) => ({
    pin: { ...body, id: 1, available: true, is_dir: false },
  }));
  render(Workspace);
  await openActions('plan #1.md');
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Pin' }));
  expect(api.putWorkspacePin).toHaveBeenCalledWith({
    provider: 'workspace',
    ref: '/Users/alice/notes/plan #1.md',
    name: 'plan #1.md',
    mime_type: null,
    web_url: null,
  });
  await openActions('plan #1.md');
  const unpin = await screen.findByRole('menuitem', { name: 'Unpin' });
  await fireEvent.keyDown(unpin, { key: 'Escape' });
  const pinned = screen.getByRole('region', { name: 'Pinned files' });
  await fireEvent.click(within(pinned).getByRole('button', { name: 'plan #1.md' }));
  expect(openFile).toHaveBeenCalledWith('/Users/alice/notes/plan #1.md');
  await fireEvent.click(within(pinned).getByRole('button', { name: 'Remove pin plan #1.md' }));
  expect(api.deleteWorkspacePin).toHaveBeenCalledWith(1);
  await openActions('plan #1.md');
  await screen.findByRole('menuitem', { name: 'Pin' });
});

it('keeps unavailable pins removable and reports failed mutations', async () => {
  api.getWorkspacePins.mockResolvedValue({
    pins: [
      {
        id: 2,
        provider: 'workspace',
        ref: '/Users/alice/gone.md',
        name: 'Gone',
        available: false,
        is_dir: false,
      },
    ],
  });
  api.deleteWorkspacePin.mockRejectedValueOnce(new Error('Try again.'));
  render(Workspace);
  await screen.findByText('Unavailable');
  const pinned = screen.getByRole('region', { name: 'Pinned files' });
  expect(within(pinned).queryByRole('button', { name: 'Gone' })).toBeNull();
  await fireEvent.click(within(pinned).getByRole('button', { name: 'Remove pin Gone' }));
  await screen.findByText('Try again.');
  expect(within(pinned).getByText('Gone')).toBeTruthy();
  await fireEvent.click(within(pinned).getByRole('button', { name: 'Remove pin Gone' }));
  await waitFor(() => expect(screen.queryByText('Gone')).toBeNull());
});

it('pins a Drive item with its provider metadata', async () => {
  api.getWorkspacePlaces.mockResolvedValue({
    places: [{ ...home, id: 'drive', kind: 'drive', label: 'Google Drive', view_root: null }],
    notes: [],
  });
  __history.reset('/istota/workspace?place=drive');
  const item = {
    id: 'drive_file_123',
    name: '<b>Budget</b>',
    mime_type: 'application/pdf',
    web_url: 'https://drive.google.com/file/d/drive_file_123',
    modified: null,
    size: null,
    is_folder: false,
  };
  api.getWorkspaceDriveListing.mockResolvedValue({ items: [item], next_page_token: null });
  api.putWorkspacePin.mockImplementation(async (body) => ({
    pin: { ...body, id: 3, available: true, is_dir: false },
  }));
  render(Workspace);
  await openActions('<b>Budget</b>');
  const providerLink = await screen.findByRole('menuitem', { name: 'Open in Google Drive' });
  expect(providerLink).toHaveAttribute('href', item.web_url);
  expect(providerLink).toHaveAttribute('target', '_blank');
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Pin' }));
  expect(api.putWorkspacePin).toHaveBeenCalledWith({
    provider: 'drive',
    ref: item.id,
    name: item.name,
    mime_type: item.mime_type,
    web_url: item.web_url,
  });
  const pinned = screen.getByRole('region', { name: 'Pinned files' });
  const link = within(pinned).getByRole('link', { name: '<b>Budget</b>' });
  expect(link.querySelector('b')).toBeNull();
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
});

it('opens and pins a linked single file using its exact server path', async () => {
  const ref = '/linked/report.txt';
  api.getWorkspacePlaces.mockResolvedValue({
    places: [{ ...home, id: 'linked:1', kind: 'linked', view_root: ref }],
    notes: [],
  });
  api.getWorkspaceListing.mockResolvedValue({
    place: 'linked:1',
    path: '',
    entries: [{ ...entry('report.txt'), view_path: ref }],
    truncated: false,
    skipped_names: 0,
  });
  api.putWorkspacePin.mockResolvedValue({
    pin: {
      id: 1,
      provider: 'workspace',
      ref,
      name: 'report.txt',
      available: true,
      is_dir: false,
    },
  });
  render(Workspace);
  await fireEvent.click(await screen.findByRole('button', { name: 'report.txt' }));
  expect(openFile).toHaveBeenCalledWith(ref);
  await openActions('report.txt');
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Pin' }));
  expect(api.putWorkspacePin).toHaveBeenCalledWith(expect.objectContaining({ ref }));
});

it('describes group access as depending on the shared room', async () => {
  api.getWorkspacePlaces.mockResolvedValue({
    places: [
      {
        ...home,
        id: 'group:team',
        kind: 'group',
        label: 'Team',
        reach: {
          ...home.reach,
          shared_member: 'conditional',
          note: 'Only when every reader belongs to this group.',
        },
      },
    ],
    notes: [],
  });
  render(Workspace);
  await screen.findByText('Depends on room');
  expect(screen.getByText('Only when every reader belongs to this group.')).toBeTruthy();
});

it('offers file downloads and keeps non-regular entries without actions', async () => {
  render(Workspace);
  await openActions('notes & drafts');
  expect(screen.queryByRole('menuitem', { name: 'Download' })).toBeNull();
  await fireEvent.keyDown(screen.getByRole('menuitem', { name: 'Pin' }), { key: 'Escape' });
  expect(screen.queryByRole('button', { name: 'Actions for outside' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Actions for socket' })).toBeNull();
  await fireEvent.click(screen.getByRole('link', { name: 'notes & drafts' }));
  await openActions('plan #1.md');
  const download = await screen.findByRole('menuitem', { name: 'Download' });
  expect(download).toHaveAttribute('download', 'plan #1.md');
  expect(download.getAttribute('href')).toContain(
    encodeURIComponent('/Users/alice/notes & drafts/plan #1.md'),
  );
});

it('sorts folders first, toggles date order and remembers it', async () => {
  api.getWorkspaceListing.mockResolvedValue({
    place: 'home',
    path: '',
    entries: [
      { ...entry('z.txt'), modified: 100 },
      { ...entry('a.txt'), modified: 200 },
      entry('Folder', 'dir'),
    ],
    truncated: false,
    skipped_names: 0,
  });
  const { unmount } = render(Workspace);
  const table = await screen.findByRole('table', { name: 'Files' });
  const names = () =>
    within(table)
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.textContent);
  expect(names()[0]).toContain('Folder');
  expect(names()[1]).toContain('a.txt');
  await fireEvent.click(screen.getByRole('button', { name: 'Sort by modified' }));
  expect(names()[1]).toContain('a.txt');
  await fireEvent.click(screen.getByRole('button', { name: 'Sort by modified' }));
  expect(names()[1]).toContain('z.txt');
  unmount();
  render(Workspace);
  const restored = await screen.findByRole('table', { name: 'Files' });
  expect(within(restored).getAllByRole('row')[2]).toHaveTextContent('z.txt');
  expect(screen.getByRole('button', { name: 'Sort by modified' }).closest('th')).toHaveAttribute(
    'aria-sort',
    'ascending',
  );
});

it('selects only visible files and confirms deletion, retaining failed items', async () => {
  api.getWorkspaceListing.mockResolvedValue({
    place: 'home',
    path: '',
    entries: [entry('a.txt'), entry('b.txt'), entry('.hidden', 'file', true)],
    truncated: false,
    skipped_names: 0,
  });
  api.deleteWorkspaceEntry = vi
    .fn()
    .mockResolvedValueOnce({ status: 'ok' })
    .mockRejectedValueOnce(new Error('Permission denied.'));
  render(Workspace);
  await fireEvent.click(await screen.findByRole('checkbox', { name: 'Select all visible items' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }));
  expect(api.deleteWorkspaceEntry).not.toHaveBeenCalled();
  await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(api.deleteWorkspaceEntry).toHaveBeenCalledTimes(2));
  expect(api.deleteWorkspaceEntry).toHaveBeenNthCalledWith(1, '', 'a.txt');
  expect(api.deleteWorkspaceEntry).toHaveBeenNthCalledWith(2, '', 'b.txt');
  await waitFor(() => expect(screen.queryByRole('button', { name: 'a.txt' })).toBeNull());
  expect(screen.getByRole('checkbox', { name: 'Select b.txt' })).toBeChecked();
  expect(screen.getByRole('alert')).toHaveTextContent('b.txt: Permission denied.');
});

it('clears selection when filtering and never exposes deletion in shared places', async () => {
  api.getWorkspaceListing.mockImplementation(async (place) => ({
    place,
    path: '',
    entries: [entry('a.txt'), entry('b.txt')],
    truncated: false,
    skipped_names: 0,
  }));
  render(Workspace);
  await fireEvent.click(await screen.findByRole('checkbox', { name: 'Select a.txt' }));
  await fireEvent.input(screen.getByRole('textbox', { name: 'Filter files' }), {
    target: { value: 'b.txt' },
  });
  expect(screen.queryByRole('button', { name: 'a.txt' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Delete selected' })).toBeDisabled();
  await fireEvent.click(screen.getByRole('link', { name: 'Project room' }));
  await screen.findByRole('button', { name: 'a.txt' });
  expect(screen.queryByRole('button', { name: 'Delete selected' })).toBeNull();
});

it('cancels deletion without a request and clears selection on navigation', async () => {
  api.deleteWorkspaceEntry = vi.fn();
  render(Workspace);
  await fireEvent.click(await screen.findByRole('checkbox', { name: 'Select notes & drafts' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }));
  await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
  expect(api.deleteWorkspaceEntry).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('link', { name: 'notes & drafts' }));
  await screen.findByRole('button', { name: 'plan #1.md' });
  expect(screen.getByRole('checkbox', { name: 'Select plan #1.md' })).not.toBeChecked();
  expect(screen.getByRole('button', { name: 'Delete selected' })).toBeDisabled();
});
