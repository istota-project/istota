/**
 * The folder picker browses My files one level at a time, shows folders only,
 * can make a folder where it is, and answers with a path relative to My files.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { WorkspaceEntry } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

function entry(name: string, kind: WorkspaceEntry['kind'] = 'dir', hidden = false) {
  return { name, kind, size: null, modified: 0, hidden };
}

const tree = vi.hoisted(() => ({}) as Record<string, ReturnType<typeof entry>[]>);

await fillApiDouble(api, {
  getWorkspaceListing: vi.fn(async (_place: string, path = '') => {
    const entries = tree[path];
    if (!entries) throw Object.assign(new Error('Folder not found.'), { status: 404 });
    return { place: 'home', path, entries, truncated: false, skipped_names: 0 };
  }),
  createWorkspaceFolder: vi.fn(async (path: string, name: string) => {
    const made = path ? `${path}/${name}` : name;
    tree[path].push(entry(name));
    tree[made] = [];
    return { path: made };
  }),
});

import FolderPicker from './FolderPicker.svelte';

beforeEach(() => {
  for (const key of Object.keys(tree)) delete tree[key];
  tree[''] = [entry('Notes'), entry('report.pdf', 'file'), entry('.trash', 'dir', true)];
  tree['Notes'] = [entry('Inbox'), entry('todo.md', 'file')];
  tree['Notes/Inbox'] = [];
  vi.clearAllMocks();
});

afterEach(() => cleanup());

function folders() {
  return within(screen.getByRole('list', { name: 'Folders' }));
}

describe('FolderPicker', () => {
  it('lists folders only and navigates into one', async () => {
    const onPick = vi.fn();
    render(FolderPicker, { open: true, onPick });
    await screen.findByRole('button', { name: 'Notes' });
    expect(screen.queryByText('report.pdf')).toBeNull();
    expect(screen.queryByText('.trash')).toBeNull();
    // The root of My files is not a folder you can choose.
    expect(screen.getByRole('button', { name: 'Choose this folder' })).toBeDisabled();

    await fireEvent.click(folders().getByRole('button', { name: 'Notes' }));
    await screen.findByRole('button', { name: 'Inbox' });
    expect(screen.queryByText('todo.md')).toBeNull();
    expect(api.getWorkspaceListing).toHaveBeenLastCalledWith('home', 'Notes');

    await fireEvent.click(folders().getByRole('button', { name: 'Inbox' }));
    await screen.findByText('No folders here.');
    const crumbs = within(screen.getByRole('navigation', { name: 'Folder path' }));
    expect(crumbs.getByText('Inbox')).toHaveAttribute('aria-current', 'location');

    await fireEvent.click(screen.getByRole('button', { name: 'Choose this folder' }));
    expect(onPick).toHaveBeenCalledWith('Notes/Inbox');
  });

  it('starts at the current value, and goes back up through the path', async () => {
    render(FolderPicker, { open: true, start: '/Notes/Inbox/', onPick: vi.fn() });
    await screen.findByText('No folders here.');
    expect(api.getWorkspaceListing).toHaveBeenCalledWith('home', 'Notes/Inbox');

    const crumbs = within(screen.getByRole('navigation', { name: 'Folder path' }));
    await fireEvent.click(crumbs.getByRole('button', { name: 'My files' }));
    await screen.findByRole('button', { name: 'Notes' });
  });

  it('opens at the root when the stored folder does not exist yet', async () => {
    render(FolderPicker, { open: true, start: 'Notes/Later', onPick: vi.fn() });
    await screen.findByRole('button', { name: 'Notes' });
    expect(api.getWorkspaceListing).toHaveBeenLastCalledWith('home', '');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('makes a folder where it is and opens it', async () => {
    const onPick = vi.fn();
    render(FolderPicker, { open: true, start: 'Notes', onPick });
    await screen.findByRole('button', { name: 'Inbox' });

    await fireEvent.click(screen.getByRole('button', { name: 'New folder' }));
    await fireEvent.input(screen.getByLabelText('New folder name'), {
      target: { value: ' Ideas ' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(api.createWorkspaceFolder).toHaveBeenCalledWith('Notes', 'Ideas'));
    const crumbs = within(screen.getByRole('navigation', { name: 'Folder path' }));
    await waitFor(() => expect(crumbs.getByText('Ideas')).toHaveAttribute('aria-current'));

    await fireEvent.click(screen.getByRole('button', { name: 'Choose this folder' }));
    expect(onPick).toHaveBeenCalledWith('Notes/Ideas');
  });

  it('keeps a refused folder name beside its field', async () => {
    api.createWorkspaceFolder.mockRejectedValueOnce(
      Object.assign(new Error('A file or folder already exists.'), { status: 409 }),
    );
    render(FolderPicker, { open: true, start: 'Notes', onPick: vi.fn() });
    await screen.findByRole('button', { name: 'Inbox' });
    await fireEvent.click(screen.getByRole('button', { name: 'New folder' }));
    await fireEvent.input(screen.getByLabelText('New folder name'), {
      target: { value: 'Inbox' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    expect(await screen.findByText('A file or folder already exists.')).toBeInTheDocument();
    expect(screen.getByLabelText('New folder name')).toHaveValue('Inbox');
  });
});
