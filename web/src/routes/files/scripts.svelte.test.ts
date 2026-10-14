import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { __history } from '../../../vitest-stubs/app-navigation';
import type { WorkspacePlace, WorkspaceScriptsIndex } from '$lib/api';
import Files from './+page.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
const openFile = vi.hoisted(() => vi.fn());
vi.mock('$lib/fileViewer/store.svelte', () => ({ viewer: { openFile } }));
vi.mock('$lib/api', () => api);
vi.mock('$lib/userContext', () => ({
  getCurrentUser: () => ({ user: { username: 'alice', bot_name: 'Helper' } }),
}));
await fillApiDouble(api);

const home: WorkspacePlace = {
  id: 'home',
  kind: 'home',
  label: 'My files',
  browsable: true,
  view_root: '/Users/alice',
  reach: { private: 'rw', shared_member: 'rw', restricted: 'none', note: null },
};
const scripts: WorkspaceScriptsIndex = {
  scripts_view_path: '/Users/alice/Helper/scripts',
  files: [
    {
      path: 'weekly.py',
      kind: 'script',
      size: 128,
      modified: '2026-10-01T09:00:00Z',
      description: 'Emails the weekly summary',
      referenced_by: [],
      mentioned_in: [],
    },
    {
      path: 'tools/report & notes.py',
      kind: 'script',
      size: 64,
      modified: '2026-10-01T09:00:00Z',
      description: null,
      referenced_by: [],
      mentioned_in: [],
    },
    {
      path: 'prompts/daily.txt',
      kind: 'prompt',
      size: 32,
      modified: '2026-10-01T09:00:00Z',
      description: 'Prepare the daily summary',
      referenced_by: [],
      mentioned_in: [],
    },
  ],
  broken: [
    {
      path: 'missing.sh',
      referenced_by: [
        { source: 'heartbeat', name: 'backup', field: 'command', kind: 'runs', state: 'enabled' },
      ],
    },
  ],
  truncated: false,
  sources: { scripts: 'ok', cron: 'ok', heartbeat: 'ok' },
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  __history.reset('/istota/files');
  api.getWorkspacePins.mockResolvedValue({ pins: [] });
  api.getWorkspaceConfigFiles.mockResolvedValue({ files: [] });
  api.getWorkspaceScripts.mockResolvedValue(scripts);
  api.getWorkspacePlaces.mockResolvedValue({
    places: [home, { ...home, id: 'linked:1', kind: 'linked', label: 'Shared folder' }],
    notes: [],
  });
  api.getWorkspaceListing.mockImplementation(async (place, path) => ({
    place,
    path,
    entries: [],
    truncated: false,
    skipped_names: 0,
  }));
});
afterEach(cleanup);

function botSection() {
  return screen.findByRole('region', { name: 'Helper' });
}

it('lists Scripts and Prompt files in the bot section, with no per-script rows', async () => {
  render(Files);
  const section = await botSection();
  const scriptsLink = await within(section).findByRole('link', { name: /^Scripts/ });
  const promptsLink = await within(section).findByRole('link', { name: /^Prompt files/ });
  const config = within(section).getByRole('link', { name: 'Configuration' });
  expect(
    config.compareDocumentPosition(scriptsLink) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(scriptsLink).toHaveAttribute('href', '/files?view=scripts');
  expect(promptsLink).toHaveAttribute('href', '/files?view=prompts');
  expect(
    within(scriptsLink).getByRole('img', { name: 'Jobs refer to missing scripts' }),
  ).toBeTruthy();
  expect(within(promptsLink).queryByRole('img')).toBeNull();
  expect(screen.queryByRole('link', { name: 'weekly.py' })).toBeNull();
  expect(screen.queryByRole('region', { name: 'Scripts' })).toBeNull();
  expect(api.getWorkspaceScripts).toHaveBeenCalledTimes(1);
});

it('hides Prompt files when there are none and keeps Scripts for an empty folder', async () => {
  api.getWorkspaceScripts.mockResolvedValue({ ...scripts, files: [], broken: [] });
  render(Files);
  const section = await botSection();
  const scriptsLink = await within(section).findByRole('link', { name: /^Scripts/ });
  expect(within(section).queryByRole('link', { name: /^Prompt files/ })).toBeNull();
  expect(within(scriptsLink).queryByRole('img')).toBeNull();
  await fireEvent.click(scriptsLink);
  expect(window.location.search).toBe('?view=scripts');
  expect(scriptsLink).toHaveAttribute('aria-current', 'page');
  expect(await screen.findByText('No scripts yet.')).toBeTruthy();
});

it('shows only scripts in the Scripts view and only prompts in the Prompt files view', async () => {
  render(Files);
  const section = await botSection();
  await fireEvent.click(await within(section).findByRole('link', { name: /^Scripts/ }));
  const main = await screen.findByRole('region', { name: 'Scripts' });
  expect(within(main).getByRole('button', { name: 'weekly.py' })).toBeTruthy();
  expect(within(main).getByRole('button', { name: 'tools/report & notes.py' })).toBeTruthy();
  expect(within(main).queryByText('prompts/daily.txt')).toBeNull();
  expect(within(main).getByText('No description')).toBeTruthy();
  expect(within(main).getAllByText('Not used by any job')).toHaveLength(2);
  expect(within(main).getByRole('note')).toHaveTextContent('missing.sh: HEARTBEAT.md: backup');
  expect(screen.getByRole('navigation', { name: 'Folder path' })).toHaveTextContent('Scripts');

  await fireEvent.click(within(section).getByRole('link', { name: /^Prompt files/ }));
  expect(window.location.search).toBe('?view=prompts');
  const prompts = await screen.findByRole('region', { name: 'Prompt files' });
  expect(within(prompts).getByRole('button', { name: 'prompts/daily.txt' })).toBeTruthy();
  expect(within(prompts).queryByText('weekly.py')).toBeNull();
  expect(within(prompts).queryByRole('note')).toBeNull();
});

it('opens a script in the shared viewer from its name and reveals its folder', async () => {
  __history.reset('/istota/files?view=scripts');
  render(Files);
  await fireEvent.click(await screen.findByRole('button', { name: 'weekly.py' }));
  expect(openFile).toHaveBeenCalledWith('/Users/alice/Helper/scripts/weekly.py');
  const trigger = screen.getByRole('button', { name: 'Actions for tools/report & notes.py' });
  await fireEvent.keyDown(trigger, { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Open' }));
  expect(openFile).toHaveBeenCalledWith('/Users/alice/Helper/scripts/tools/report & notes.py');
  await fireEvent.keyDown(trigger, { key: 'Enter' });
  const reveal = await screen.findByRole('menuitem', { name: 'Reveal' });
  expect(reveal).toHaveAttribute('href', '/files?place=home&path=Helper%2Fscripts%2Ftools');
  await fireEvent.click(reveal);
  await waitFor(() =>
    expect(api.getWorkspaceListing).toHaveBeenCalledWith('home', 'Helper/scripts/tools'),
  );
});

it('retries a failed index load from the main pane', async () => {
  __history.reset('/istota/files?view=scripts');
  api.getWorkspaceScripts.mockRejectedValueOnce(new Error('down'));
  render(Files);
  const main = await screen.findByRole('region', { name: 'Scripts' });
  expect(await within(main).findByText('Could not load your scripts.')).toBeTruthy();
  await fireEvent.click(within(main).getByRole('button', { name: 'Retry' }));
  expect(await within(main).findByRole('button', { name: 'weekly.py' })).toBeTruthy();
});
