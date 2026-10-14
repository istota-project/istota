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
  HTMLElement.prototype.scrollIntoView = vi.fn();
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

async function openScripts() {
  const sidebar = await screen.findByRole('complementary');
  const section = await within(sidebar).findByRole('region', { name: 'Scripts' });
  await fireEvent.click(within(section).getByRole('button', { name: /Scripts/ }));
  return section;
}

it('places a collapsed Scripts section after the bot configuration and before linked folders', async () => {
  render(Files);
  const sidebar = await screen.findByRole('complementary');
  const section = await within(sidebar).findByRole('region', { name: 'Scripts' });
  const config = screen.getByRole('link', { name: 'Configuration' });
  const linked = screen.getByRole('button', { name: /Linked folders/ });
  expect(config.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(section.compareDocumentPosition(linked) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(section).queryByRole('link')).toBeNull();
  await openScripts();
  const all = await within(section).findByRole('link', { name: 'All scripts' });
  expect(all).toHaveTextContent('2');
  expect(within(all).getByRole('img', { name: 'Broken script references' })).toBeTruthy();
  expect(within(section).getByRole('link', { name: 'weekly.py' })).toHaveAccessibleDescription(
    'Emails the weekly summary',
  );
  expect(
    within(section).getByRole('link', { name: 'tools/report & notes.py' }),
  ).toHaveAccessibleDescription('No description');
  expect(within(section).queryByText('prompts/daily.txt')).toBeNull();
  expect(api.getWorkspaceScripts).toHaveBeenCalledTimes(1);
});

it('remembers whether the Scripts section is open', async () => {
  const first = render(Files);
  await openScripts();
  expect(localStorage.getItem('workspace:section:scripts')).toBe('true');
  first.unmount();
  render(Files);
  const sidebar = await screen.findByRole('complementary');
  const section = await within(sidebar).findByRole('region', { name: 'Scripts' });
  expect(await within(section).findByRole('link', { name: 'All scripts' })).toBeTruthy();
});

it('navigates to all scripts and individual scripts with ordinary links and shallow history', async () => {
  render(Files);
  const section = await openScripts();
  const all = await within(section).findByRole('link', { name: 'All scripts' });
  expect(all).toHaveAttribute('href', '/files?view=scripts');
  await fireEvent.click(all);
  expect(window.location.search).toBe('?view=scripts');
  expect(all).toHaveAttribute('aria-current', 'page');
  const row = within(section).getByRole('link', { name: 'tools/report & notes.py' });
  expect(row).toHaveAttribute('href', '/files?view=scripts&script=tools%2Freport+%26+notes.py');
  await fireEvent.click(row);
  expect(new URLSearchParams(window.location.search).get('script')).toBe('tools/report & notes.py');
  expect(row).toHaveAttribute('aria-current', 'page');
  expect(all).not.toHaveAttribute('aria-current');
  expect(screen.getByRole('navigation', { name: 'Folder path' })).toHaveTextContent('Scripts');
  expect(screen.queryByRole('region', { name: 'Skill instructions' })).toBeNull();
  __history.back();
  window.dispatchEvent(new PopStateEvent('popstate'));
  await waitFor(() => expect(all).toHaveAttribute('aria-current', 'page'));
  __history.forward();
  window.dispatchEvent(new PopStateEvent('popstate'));
  await waitFor(() => expect(row).toHaveAttribute('aria-current', 'page'));
});

it('restores a script deep link without requesting a folder listing', async () => {
  __history.reset('/istota/files?view=scripts&script=weekly.py');
  render(Files);
  const section = await openScripts();
  expect(await within(section).findByRole('link', { name: 'weekly.py' })).toHaveAttribute(
    'aria-current',
    'page',
  );
  expect(window.location.search).toBe('?view=scripts&script=weekly.py');
  expect(api.getWorkspaceListing).not.toHaveBeenCalled();
});

it.each(['All scripts', 'Configuration', 'My files', 'Shared folder'])(
  'clears the script selection when navigating to %s',
  async (destination) => {
    __history.reset('/istota/files?view=scripts&script=weekly.py&other=keep');
    render(Files);
    const section = await openScripts();
    await within(section).findByRole('link', { name: 'weekly.py' });
    if (destination === 'Shared folder') {
      await fireEvent.click(screen.getByRole('button', { name: /Linked folders/ }));
    }
    await fireEvent.click(screen.getByRole('link', { name: destination }));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).has('script')).toBe(false),
    );
    expect(new URLSearchParams(window.location.search).get('other')).toBe('keep');
  },
);

it.each(['view=config', 'place=home'])('ignores script outside its view: %s', async (query) => {
  __history.reset(`/istota/files?${query}&script=weekly.py`);
  render(Files);
  await screen.findByRole('complementary');
  await waitFor(() =>
    expect(new URLSearchParams(window.location.search).has('script')).toBe(false),
  );
});

it('keeps scripts load failures visible and retryable', async () => {
  api.getWorkspaceScripts.mockRejectedValueOnce(new Error('down'));
  render(Files);
  const section = await openScripts();
  expect(await within(section).findByText('Could not load your scripts.')).toBeTruthy();
  expect(within(section).queryByText('0')).toBeNull();
  await fireEvent.click(within(section).getByRole('button', { name: 'Retry scripts' }));
  expect(await within(section).findByRole('link', { name: 'weekly.py' })).toBeTruthy();
});

it('keeps All scripts available for an empty index without a warning', async () => {
  api.getWorkspaceScripts.mockResolvedValue({ ...scripts, files: [], broken: [] });
  render(Files);
  const section = await openScripts();
  const all = await within(section).findByRole('link', { name: 'All scripts' });
  expect(all).toHaveTextContent('0');
  expect(within(section).getAllByRole('link')).toHaveLength(1);
  expect(within(section).queryByRole('img', { name: 'Broken script references' })).toBeNull();
  await fireEvent.click(all);
  expect(window.location.search).toBe('?view=scripts');
});

it('leaves modified clicks to the browser', async () => {
  render(Files);
  const section = await openScripts();
  const row = await within(section).findByRole('link', { name: 'weekly.py' });
  const query = window.location.search;
  const event = new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true });
  await fireEvent(row, event);
  expect(event.defaultPrevented).toBe(false);
  expect(window.location.search).toBe(query);
  expect(row).not.toHaveAttribute('aria-current');
});

it('shows missing references, scripts and prompt files in the main pane', async () => {
  render(Files);
  const section = await openScripts();
  await fireEvent.click(await within(section).findByRole('link', { name: 'All scripts' }));
  const table = await screen.findByRole('table', { name: 'Scripts' });
  expect(within(table).getAllByText('Not referenced')).toHaveLength(2);
  expect(within(table).getByText('No description')).toBeTruthy();
  expect(screen.getByRole('note')).toHaveTextContent('missing.sh');
  expect(screen.getByRole('note')).toHaveTextContent('heartbeat: backup');
  expect(screen.getByRole('note')).toHaveTextContent('enabled');
  expect(screen.getByRole('table', { name: 'Prompt files' })).toHaveTextContent(
    'prompts/daily.txt',
  );
});

it('expands and scrolls a deep-linked row after the index arrives and restores history', async () => {
  __history.reset('/istota/files?view=scripts&script=weekly.py');
  let resolve!: (value: WorkspaceScriptsIndex) => void;
  api.getWorkspaceScripts.mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const scrolled: Element[] = [];
  const scroll = vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(function (
    this: HTMLElement,
  ) {
    scrolled.push(this);
  });
  render(Files);
  await screen.findByText('Loading scripts…');
  resolve(scripts);
  const first = await screen.findByRole('button', { name: 'weekly.py' });
  await waitFor(() => expect(first).toHaveAttribute('aria-expanded', 'true'));
  await waitFor(() => expect(scrolled.at(-1)).toContainElement(first));
  const second = screen.getByRole('button', { name: 'tools/report & notes.py' });
  await fireEvent.click(second);
  expect(first).toHaveAttribute('aria-expanded', 'false');
  expect(second).toHaveAttribute('aria-expanded', 'true');
  expect(new URLSearchParams(window.location.search).get('script')).toBe('tools/report & notes.py');
  __history.back();
  window.dispatchEvent(new PopStateEvent('popstate'));
  await waitFor(() => expect(first).toHaveAttribute('aria-expanded', 'true'));
  await waitFor(() => expect(scrolled.at(-1)).toContainElement(first));
  __history.forward();
  window.dispatchEvent(new PopStateEvent('popstate'));
  await waitFor(() => expect(second).toHaveAttribute('aria-expanded', 'true'));
  await waitFor(() => expect(scrolled.at(-1)).toContainElement(second));
  scroll.mockRestore();
});

it('opens scripts in the shared viewer and reveals their containing folder', async () => {
  __history.reset('/istota/files?view=scripts');
  render(Files);
  const trigger = await screen.findByRole('button', {
    name: 'Actions for tools/report & notes.py',
  });
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
  expect(new URLSearchParams(window.location.search).has('script')).toBe(false);
});

it('retries a failed main-pane index load', async () => {
  __history.reset('/istota/files?view=scripts');
  api.getWorkspaceScripts.mockRejectedValueOnce(new Error('down'));
  render(Files);
  const main = await screen.findByRole('region', { name: 'Scripts view' });
  expect(await within(main).findByText('Could not load your scripts.')).toBeTruthy();
  await fireEvent.click(within(main).getByRole('button', { name: 'Retry' }));
  expect(await within(main).findByRole('table', { name: 'Scripts' })).toBeTruthy();
});
