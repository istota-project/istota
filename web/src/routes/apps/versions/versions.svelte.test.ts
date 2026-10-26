/**
 * Versions: every installed version with its dates and what it asks for,
 * "Make current" (the rollback) and delete for a version that is not current.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import type { AppsList, AppVersions } from '$lib/apps/api';

const apps = vi.hoisted(() => ({
  getApps: vi.fn(),
  getVersions: vi.fn(),
  makeCurrent: vi.fn(async () => ({ app_id: 'tides', current_version: 1 })),
  deleteVersion: vi.fn(async () => {}),
}));
vi.mock('$lib/apps/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/apps/api')>()),
  ...apps,
}));
const identity = vi.hoisted(() => ({
  user: { username: 'alice', bot_name: 'Istota', features: { apps: true } },
  expireSession: vi.fn(),
  reload: vi.fn(),
}));
vi.mock('$lib/userContext', () => ({ getCurrentUser: () => identity }));

const Harness = (await import('./versionsInLayout.test.svelte')).default;

const LIST: AppsList = {
  apps: [
    {
      app_id: 'tides',
      name: 'Tides',
      icon: 'chart-line',
      description: '',
      origin: 'draft',
      current_version: 2,
      narrowest: 'phone',
      pending_id: null,
      ungranted: [],
    },
  ],
  pending: [],
  drafts: [],
  starter: [],
  kit_built: true,
};

function versions(): AppVersions {
  return {
    app_id: 'tides',
    current_version: 2,
    versions: [
      {
        version: 1,
        current: false,
        installed_at: '2026-10-20T10:00:00Z',
        approved_at: '2026-10-20T10:01:00Z',
        narrowest: 'phone',
        capabilities: ['kv:own', 'files:own'],
        grants: ['kv:own', 'files:own'],
      },
      {
        version: 2,
        current: true,
        installed_at: '2026-10-24T10:00:00Z',
        approved_at: '2026-10-24T10:01:00Z',
        narrowest: 'phone',
        capabilities: ['kv:own', 'files:own', 'calendar:read'],
        grants: ['kv:own', 'files:own', 'calendar:read'],
      },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  page.url = new URL('http://localhost/apps/versions/?id=tides');
  apps.getApps.mockResolvedValue(LIST);
  apps.getVersions.mockResolvedValue(versions());
});
afterEach(() => cleanup());

function card(name: string): HTMLElement {
  return screen.getByRole('heading', { name }).closest('section') as HTMLElement;
}

describe('Versions', () => {
  it('lists the versions newest first, the current one marked and without actions', async () => {
    render(Harness);
    await screen.findByRole('heading', { name: 'Version 2' });
    const headings = screen
      .getAllByRole('heading', { level: 2 })
      .map((h) => h.textContent?.trim())
      .filter((t) => t?.startsWith('Version '));
    expect(headings).toEqual(['Version 2', 'Version 1']);
    const current = within(card('Version 2'));
    expect(current.getByText('Current')).toBeTruthy();
    expect(current.getByText('calendar:read')).toBeTruthy();
    expect(current.queryByRole('button')).toBeNull();
    expect(within(card('Version 1')).getByText('Only its own data and files.')).toBeTruthy();
    expect(screen.getByRole('link', { name: /Back to Tides/ })).toHaveAttribute(
      'href',
      '/apps/open/?id=tides',
    );
  });

  it('makes an earlier version current', async () => {
    render(Harness);
    await fireEvent.click(await screen.findByRole('button', { name: 'Make version 1 current' }));
    await waitFor(() => expect(apps.makeCurrent).toHaveBeenCalledWith('tides', 1));
    await waitFor(() => expect(apps.getVersions).toHaveBeenCalledTimes(2));
  });

  it('deletes a version that is not current, after confirming', async () => {
    render(Harness);
    await fireEvent.click(await screen.findByRole('button', { name: 'Delete version 1' }));
    const dialog = await screen.findByRole('dialog');
    expect(apps.deleteVersion).not.toHaveBeenCalled();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(apps.deleteVersion).toHaveBeenCalledWith('tides', 1));
  });

  it('says which capabilities a version asks for and was not allowed', async () => {
    const data = versions();
    data.versions[1].grants = ['kv:own', 'files:own'];
    apps.getVersions.mockResolvedValue(data);
    render(Harness);
    await screen.findByRole('heading', { name: 'Version 2' });
    expect(within(card('Version 2')).getByText('not allowed')).toBeTruthy();
  });
});
