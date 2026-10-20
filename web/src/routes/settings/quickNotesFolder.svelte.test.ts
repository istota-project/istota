/**
 * Preferences → Quick notes. The folder is picked in the folder dialog, held
 * in the settings layout's one draft, and saved by the app-bar Save with only
 * this field in the patch. Clearing sends null, which the server stores as
 * unset.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { User, UserProfile } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';

function profile(folder: string | null, managed: string[] = []): UserProfile {
  return {
    managed,
    user_id: 'alice',
    display_name: 'Alice',
    timezone: 'UTC',
    email_addresses: [],
    trusted_email_senders: [],
    quiet_email_senders: [],
    log_channel: '',
    alerts_channel: '',
    disabled_skills: [],
    disabled_modules: [],
    max_foreground_workers: 0,
    max_background_workers: 0,
    routing: {},
    default_destination: 'talk',
    default_room: '',
    briefing_email_html: true,
    timezone_follow_location: false,
    show_nav_unread_badges: false,
    external_turn_display: 'collapsed',
    relay_delivery: '',
    quick_notes_folder: folder,
  };
}

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const current = vi.hoisted(() => ({ profile: null as UserProfile | null }));
await fillApiDouble(api, {
  getModules: vi.fn(async () => ({ modules: [] })),
  getProfile: vi.fn(async () => ({ profile: current.profile })),
  updateProfile: vi.fn(async () => ({ ok: true, fields: [] })),
  getWorkspaceListing: vi.fn(async (_place: string, path = '') => ({
    place: 'home',
    path,
    entries:
      path === ''
        ? [{ name: 'Notes', kind: 'dir', size: null, modified: 0, hidden: false }]
        : path === 'Notes'
          ? [{ name: 'Inbox', kind: 'dir', size: null, modified: 0, hidden: false }]
          : [],
    truncated: false,
    skipped_names: 0,
  })),
});

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  shellRunsServiceWorker: vi.fn(() => false),
  deviceModel: vi.fn(() => 'iPhone'),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import Page from './preferences/+page.svelte';
import SettingsLayout from './+layout.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

const person: User = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: false,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: false,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: false,
  },
};

async function mount() {
  render(Harness, { component: Page, layout: SettingsLayout, user: person });
  await screen.findByRole('heading', { name: 'Quick notes' });
}

function folderInput() {
  return screen.getByLabelText('Folder') as HTMLInputElement;
}

beforeEach(() => {
  current.profile = profile(null);
  vi.clearAllMocks();
});

afterEach(() => cleanup());

describe('the Quick notes card', () => {
  it('picks a folder in the dialog and saves only that field', async () => {
    await mount();
    expect(folderInput()).toHaveValue('');
    expect(folderInput()).toHaveAttribute('placeholder', 'Not set');
    expect(document.getElementById('quick-notes')).not.toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: 'Choose…' }));
    const dialog = within(await screen.findByRole('dialog'));
    await fireEvent.click(await dialog.findByRole('button', { name: 'Notes' }));
    await fireEvent.click(await dialog.findByRole('button', { name: 'Inbox' }));
    await dialog.findByText('No folders here.');
    await fireEvent.click(dialog.getByRole('button', { name: 'Choose this folder' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(folderInput()).toHaveValue('Notes/Inbox');
    await waitFor(() => expect(get(settingsSave)?.dirty).toBe(true));
    await get(settingsSave)!.save();
    await waitFor(() => expect(api.updateProfile).toHaveBeenCalledOnce());
    expect(api.updateProfile).toHaveBeenCalledWith({ quick_notes_folder: 'Notes/Inbox' });
  });

  it('opens the dialog at the stored folder and clears to null', async () => {
    current.profile = profile('Notes/Inbox');
    await mount();
    expect(folderInput()).toHaveValue('Notes/Inbox');

    await fireEvent.click(screen.getByRole('button', { name: 'Choose…' }));
    const dialog = within(await screen.findByRole('dialog'));
    await dialog.findByText('No folders here.');
    expect(api.getWorkspaceListing).toHaveBeenCalledWith('home', 'Notes/Inbox');
    await fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    await fireEvent.click(screen.getByRole('button', { name: 'Clear the quick notes folder' }));
    expect(folderInput()).toHaveValue('');
    await get(settingsSave)!.save();
    await waitFor(() => expect(api.updateProfile).toHaveBeenCalledOnce());
    expect(api.updateProfile).toHaveBeenCalledWith({ quick_notes_folder: null });
  });

  it('says why a refused folder was not saved', async () => {
    api.updateProfile.mockRejectedValueOnce(
      Object.assign(new Error('That path is not a folder in My files.'), {
        status: 422,
        field: 'quick_notes_folder',
      }),
    );
    await mount();
    await fireEvent.click(screen.getByRole('button', { name: 'Choose…' }));
    const dialog = within(await screen.findByRole('dialog'));
    await fireEvent.click(await dialog.findByRole('button', { name: 'Notes' }));
    await dialog.findByRole('button', { name: 'Inbox' });
    await fireEvent.click(dialog.getByRole('button', { name: 'Choose this folder' }));
    await waitFor(() => expect(get(settingsSave)?.dirty).toBe(true));
    await get(settingsSave)!.save();
    expect(await screen.findByText('That path is not a folder in My files.')).toBeInTheDocument();
    expect(folderInput()).toHaveValue('Notes');
  });

  it('is locked when the deployment sets it', async () => {
    current.profile = profile('Notes/Inbox', ['quick_notes_folder']);
    await mount();
    expect(screen.getByRole('button', { name: 'Choose…' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Clear the quick notes folder' })).toBeNull();
    expect(screen.getByText('Set by your administrator')).toBeInTheDocument();
  });
});
