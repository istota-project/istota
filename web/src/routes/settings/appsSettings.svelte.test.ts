/**
 * Settings → Apps: each installed app's capabilities, what it stores, and
 * Uninstall; and the section's place in the settings sidebar, which lists it
 * only for a user with the apps module.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { User } from '$lib/api';
import type { AppDetail, AppsList } from '$lib/apps/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
  getModules: vi.fn(async () => ({ modules: [] })),
  getProfile: vi.fn(async () => ({ profile: null })),
});

const apps = vi.hoisted(() => ({
  getApps: vi.fn(),
  getApp: vi.fn(),
  getAppStorage: vi.fn(),
  uninstallApp: vi.fn(async () => {}),
}));
vi.mock('$lib/apps/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/apps/api')>()),
  ...apps,
}));

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import Page from './apps/+page.svelte';
import SettingsLayout from './+layout.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import { USER_SETTINGS_SECTIONS } from '$lib/settings/sections';

function person(withApps: boolean): User {
  return {
    username: 'alice',
    display_name: 'Alice',
    bot_name: 'Istota',
    is_admin: false,
    features: { apps: withApps },
  } as unknown as User;
}

function detail(overrides: Partial<AppDetail>): AppDetail {
  return {
    app_id: 'board',
    name: 'Board',
    icon: 'kanban',
    description: '',
    origin: 'starter',
    current_version: 1,
    installed_at: '2026-10-25T10:00:00Z',
    approved_at: null,
    manifest: {
      id: 'board',
      name: 'Board',
      icon: 'kanban',
      entry: 'index.html',
      description: '',
      capabilities: ['kv:own', 'files:own'],
      narrowest: 'phone',
      reason: '',
    },
    narrowest: 'phone',
    reason: '',
    warnings: [],
    pending_id: null,
    grants: [
      { capability: 'kv:own', scope: null, description: 'Keep its own data', sensitive: false },
    ],
    ungranted: [],
    ...overrides,
  };
}

const LIST = {
  apps: [{ app_id: 'board' }, { app_id: 'recall' }],
  pending: [],
  drafts: [],
  starter: [],
  kit_built: true,
} as unknown as AppsList;

beforeEach(() => {
  vi.clearAllMocks();
  apps.getApps.mockResolvedValue(LIST);
  apps.getAppStorage.mockResolvedValue({
    apps: [
      { app_id: 'board', name: 'Board', kv_bytes: 2048, data_bytes: 0 },
      { app_id: 'recall', name: 'Recall', kv_bytes: 10, data_bytes: null },
    ],
  });
  apps.getApp.mockImplementation(async (id: string) =>
    id === 'recall'
      ? detail({
          app_id: 'recall',
          name: 'Recall',
          ungranted: [
            {
              capability: 'files:read:notes',
              scope: 'notes',
              description: 'Read your files in notes',
              sensitive: false,
            },
          ],
        })
      : detail({}),
  );
});
afterEach(() => cleanup());

function card(name: string): HTMLElement {
  return screen.getByRole('heading', { name, level: 2 }).closest('section') as HTMLElement;
}

describe('Settings → Apps', () => {
  it('is the ninth settings section, listed only with the apps module', async () => {
    expect(USER_SETTINGS_SECTIONS).toHaveLength(9);
    expect(USER_SETTINGS_SECTIONS.at(-1)).toMatchObject({ href: '/apps', label: 'Apps' });
    render(Harness, { component: Page, layout: SettingsLayout, user: person(true) });
    expect(screen.getByRole('link', { name: 'Apps' })).toHaveAttribute('href', '/settings/apps');
    cleanup();
    render(Harness, { component: Page, layout: SettingsLayout, user: person(false) });
    expect(screen.queryByRole('link', { name: 'Apps' })).toBeNull();
    expect(await screen.findByText('Apps are not turned on')).toBeTruthy();
    expect(apps.getApps).toHaveBeenCalledTimes(1);
  });

  it('shows each app with what it is allowed, what it asks for, and its storage', async () => {
    render(Harness, { component: Page, layout: SettingsLayout, user: person(true) });
    await screen.findByRole('heading', { name: 'Board', level: 2 });
    const board = within(card('Board'));
    expect(board.getByText('Keep its own data')).toBeTruthy();
    expect(board.getByText('2 KB')).toBeTruthy();
    expect(board.queryByText('Not allowed yet')).toBeNull();
    const recall = within(card('Recall'));
    expect(recall.getByText('Not allowed yet')).toBeTruthy();
    expect(recall.getByText('Read your files in notes')).toBeTruthy();
    expect(recall.getByText('Not measured')).toBeTruthy();
    expect(recall.getByRole('link', { name: 'Versions of Recall' })).toHaveAttribute(
      'href',
      '/apps/versions/?id=recall',
    );
  });

  it('uninstalls an app, keeping its data unless asked', async () => {
    render(Harness, { component: Page, layout: SettingsLayout, user: person(true) });
    await screen.findByRole('heading', { name: 'Board', level: 2 });
    await fireEvent.click(screen.getByRole('button', { name: 'Uninstall Board' }));
    const dialog = await screen.findByRole('dialog');
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Uninstall' }));
    await waitFor(() => expect(apps.uninstallApp).toHaveBeenCalledWith('board', false));
    await waitFor(() => expect(apps.getApps).toHaveBeenCalledTimes(2));
  });
});
