import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { page } from '../../../vitest-stubs/app-state.svelte';
import type { AppsList } from '$lib/apps/api';

const apps = vi.hoisted(() => ({
  getApps: vi.fn(),
  createPending: vi.fn(),
  reinstallStarter: vi.fn(),
}));
vi.mock('$lib/apps/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/apps/api')>()),
  ...apps,
}));
const navigation = vi.hoisted(() => ({ goto: vi.fn(async () => {}) }));
vi.mock('$app/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$app/navigation')>()),
  goto: navigation.goto,
}));
const identity = vi.hoisted(() => ({
  user: { username: 'alice', bot_name: 'Istota', features: { apps: true } },
  expireSession: vi.fn(),
  reload: vi.fn(),
}));
vi.mock('$lib/userContext', () => ({ getCurrentUser: () => identity }));

const { AppsApiError } = await import('$lib/apps/api');
const { AuthError } = await import('$lib/api');
const Harness = (await import('./appsInLayout.test.svelte')).default;

function list(overrides: Partial<AppsList> = {}): AppsList {
  return {
    apps: [
      {
        app_id: 'board',
        name: 'Board',
        icon: 'kanban',
        description: 'Cards and columns next to a conversation.',
        origin: 'starter',
        current_version: 2,
        narrowest: 'phone',
        pending_id: null,
        ungranted: [],
      },
      {
        app_id: 'tides',
        name: 'Tides',
        icon: 'no-such-icon',
        description: '',
        origin: 'draft',
        current_version: 1,
        narrowest: 'phone',
        pending_id: 'p-tides',
        ungranted: [],
      },
    ],
    pending: [
      {
        pending_id: 'p-tides',
        app_id: 'tides',
        name: 'Tides',
        origin: 'draft',
        size: 48_000,
        created_at: '2026-10-25T10:00:00Z',
      },
    ],
    drafts: [
      { app_id: 'packing', name: 'Packing list', has_manifest: true, state: 'new' },
      { app_id: 'scratch', name: null, has_manifest: false, state: 'new' },
    ],
    starter: [
      { app_id: 'board', state: 'installed' },
      { app_id: 'light-planner', state: 'dismissed' },
    ],
    kit_built: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  page.url = new URL('http://localhost/apps/');
  apps.getApps.mockResolvedValue(list());
});
afterEach(cleanup);

function sidebar() {
  return screen.getByRole('complementary');
}

describe('the Apps section', () => {
  it('lists the installed apps in the sidebar, each a link to its view', async () => {
    render(Harness);
    const board = await within(await waitFor(sidebar)).findByRole('link', { name: /Board/ });
    expect(board).toHaveAttribute('href', '/apps/open/?id=board');
    const tides = within(sidebar()).getByRole('link', { name: /Tides/ });
    expect(tides).toHaveAttribute('href', '/apps/open/?id=tides');
    // The waiting install is marked on its row; the other row carries no mark.
    expect(within(tides).getByRole('img', { name: 'An install is waiting' })).toBeTruthy();
    expect(within(board).queryByRole('img', { name: 'An install is waiting' })).toBeNull();
    expect(within(sidebar()).getByRole('link', { name: 'All apps' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('shows installed apps, pending installs, drafts and starter state in All apps', async () => {
    render(Harness);
    const installed = await screen.findByRole('region', { name: 'Installed' });
    expect(within(installed).getByRole('heading', { name: 'Board' })).toBeTruthy();
    expect(within(installed).getByText('Shipped with Istota')).toBeTruthy();
    expect(within(installed).getByRole('link', { name: 'Open Board' })).toHaveAttribute(
      'href',
      '/apps/open/?id=board',
    );

    const waiting = screen.getByRole('region', { name: 'Waiting for review' });
    expect(within(waiting).getByRole('link', { name: 'Review Tides' })).toHaveAttribute(
      'href',
      '/apps/install/?id=tides&pending=p-tides',
    );

    const drafts = screen.getByRole('region', { name: 'Drafts' });
    expect(within(drafts).getByRole('heading', { name: 'Packing list' })).toBeTruthy();
    // A draft with no manifest is named by its folder and says what is missing.
    expect(within(drafts).getByRole('heading', { name: 'scratch' })).toBeTruthy();
    expect(within(drafts).getByText(/no manifest\.toml/i)).toBeTruthy();

    const shipped = screen.getByRole('region', { name: 'Shipped with Istota' });
    expect(within(shipped).getByRole('heading', { name: 'Light planner' })).toBeTruthy();
    expect(within(shipped).getByRole('button', { name: 'Reinstall Light planner' })).toBeTruthy();
    expect(within(shipped).queryByRole('heading', { name: 'Board' })).toBeNull();
  });

  it('builds a pending install from a draft and opens its approval screen', async () => {
    apps.createPending.mockResolvedValue({ pending_id: 'p-new', app_id: 'packing' });
    render(Harness);
    await fireEvent.click(
      await screen.findByRole('button', { name: 'Review install of Packing list' }),
    );
    await waitFor(() => expect(apps.createPending).toHaveBeenCalledWith('packing'));
    expect(navigation.goto).toHaveBeenCalledWith('/apps/install/?id=packing&pending=p-new');
  });

  it('shows a draft that fails validation with its errors on its own card', async () => {
    apps.createPending.mockRejectedValue(
      new AppsApiError(422, 'x', ['manifest.toml: id must equal the folder name']),
    );
    render(Harness);
    await fireEvent.click(
      await screen.findByRole('button', { name: 'Review install of Packing list' }),
    );
    const card = (await screen.findByText('manifest.toml: id must equal the folder name')).closest(
      'section, article',
    ) as HTMLElement;
    expect(within(card).getByRole('heading', { name: 'Packing list' })).toBeTruthy();
    expect(navigation.goto).not.toHaveBeenCalled();
  });

  it('says so when the apps kit is not built', async () => {
    apps.getApps.mockResolvedValue(
      list({ kit_built: false, starter: [{ app_id: 'board', state: 'unavailable' }] }),
    );
    render(Harness);
    expect(await screen.findByText('The apps kit is not built')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Shipped with Istota' })).toBeNull();
  });

  it('reinstalls a starter app the user uninstalled and reloads the list', async () => {
    apps.reinstallStarter.mockResolvedValue({ app_id: 'light-planner', status: 'installed' });
    render(Harness);
    await fireEvent.click(await screen.findByRole('button', { name: 'Reinstall Light planner' }));
    await waitFor(() => expect(apps.reinstallStarter).toHaveBeenCalledWith('light-planner'));
    await waitFor(() => expect(apps.getApps).toHaveBeenCalledTimes(2));
  });

  it('shows the empty state when there is nothing installed, waiting or drafted', async () => {
    apps.getApps.mockResolvedValue(list({ apps: [], pending: [], drafts: [], starter: [] }));
    render(Harness);
    expect(await screen.findByText('No apps yet')).toBeTruthy();
  });

  it('shows a load failure with a retry', async () => {
    apps.getApps.mockRejectedValueOnce(new Error('API error: 500'));
    render(Harness);
    await fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('region', { name: 'Installed' })).toBeTruthy();
  });

  it('ends the session on an auth failure', async () => {
    apps.getApps.mockRejectedValue(new AuthError());
    render(Harness);
    await waitFor(() => expect(identity.expireSession).toHaveBeenCalled());
  });
});
