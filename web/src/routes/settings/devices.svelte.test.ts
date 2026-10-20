/**
 * Settings → Devices: the phones holding a device token for native actions.
 * The page lists them with their scopes in plain words, revokes one after a
 * confirmation, and links to location devices rather than listing them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { DeviceToken, User } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const store = vi.hoisted(() => ({ devices: [] as DeviceToken[] }));
await fillApiDouble(api, {
  getModules: vi.fn(async () => ({ modules: [] })),
  getProfile: vi.fn(async () => ({ profile: null })),
  listDevices: vi.fn(async () => ({ devices: store.devices })),
  deleteDevice: vi.fn(async (id: number) => {
    store.devices = store.devices.filter((d) => d.id !== id);
    return { ok: true };
  }),
});

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

const shellActions = vi.hoisted(() => ({ deviceId: null as string | null }));
vi.mock('$lib/platform/nativeActions', () => ({
  actionsAvailable: vi.fn(() => false),
  actionsStatus: vi.fn(async () =>
    shellActions.deviceId
      ? {
          deviceId: shellActions.deviceId,
          scopes: ['notes'],
          tokenRejected: false,
          pendingNotes: 0,
          failedNotes: 0,
        }
      : null,
  ),
}));

import Page from './devices/+page.svelte';
import DeviceTokensCard from '$lib/components/settings/DeviceTokensCard.svelte';
import SettingsLayout from './+layout.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

const PHONE = '6F9619FF-8B86-D011-B42D-00C04FC964FF';

function device(id: number, deviceId: string, label: string, tail: string): DeviceToken {
  return {
    id,
    device_id: deviceId,
    label,
    scopes: ['notes'],
    token_tail: tail,
    created_at: '2026-10-01T10:00:00Z',
    last_used_at: null,
  };
}

function person(location: boolean): User {
  return {
    username: 'alice',
    display_name: 'Alice',
    bot_name: 'Istota',
    is_admin: false,
    features: {
      chat: true,
      feeds: false,
      location,
      money: false,
      health: false,
      briefings: false,
      google_workspace: false,
      google_workspace_enabled: false,
      admin: false,
    },
  };
}

beforeEach(() => {
  store.devices = [
    device(1, PHONE, 'iPhone', 'ab12'),
    device(2, '0E984725-C51C-4BF4-9960-E1C80E27ABA0', 'iPad', 'cd34'),
  ];
  shellActions.deviceId = null;
  vi.clearAllMocks();
});

afterEach(() => cleanup());

describe('Settings → Devices', () => {
  it('lists each device with its scopes in plain words', async () => {
    render(Harness, { component: Page, layout: SettingsLayout, user: person(true) });
    expect(await screen.findByText('iPhone · …ab12')).toBeInTheDocument();
    expect(screen.getByText('iPad · …cd34')).toBeInTheDocument();
    expect(screen.getAllByText(/Quick notes · added/)).toHaveLength(2);
    expect(screen.queryByText('This device')).toBeNull();
    expect(screen.getByRole('link', { name: 'Location settings' })).toHaveAttribute(
      'href',
      '/location/settings',
    );
  });

  it('revokes a device after asking, and reloads the list', async () => {
    render(Harness, { component: Page, layout: SettingsLayout, user: person(false) });
    await fireEvent.click(await screen.findByRole('button', { name: 'Revoke iPad ending cd34' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.getByText(/iPad stops saving quick notes/)).toBeInTheDocument();
    await fireEvent.click(dialog.getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(api.deleteDevice).toHaveBeenCalledWith(2));
    await waitFor(() => expect(screen.queryByText('iPad · …cd34')).toBeNull());
    expect(screen.getByText('iPhone · …ab12')).toBeInTheDocument();
    expect(api.listDevices).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('link', { name: 'Location settings' })).toBeNull();
  });

  it('says so when there are no devices', async () => {
    store.devices = [];
    render(Harness, { component: Page, layout: SettingsLayout, user: person(false) });
    expect(await screen.findByText(/No devices yet/)).toBeInTheDocument();
  });

  it('marks the row whose device id the shell reports', async () => {
    shellActions.deviceId = PHONE;
    render(Harness, { component: Page, layout: SettingsLayout, user: person(false) });
    const row = (await screen.findByText('iPhone · …ab12')).closest('li') as HTMLElement;
    expect(await within(row).findByText('This device')).toBeInTheDocument();
    expect(screen.getAllByText('This device')).toHaveLength(1);
  });

  it('marks the phone the page runs on as this device', async () => {
    render(DeviceTokensCard, {
      devices: store.devices,
      thisDeviceId: PHONE.toLowerCase(),
      onChanged: vi.fn(),
    });
    const row = screen.getByText('iPhone · …ab12').closest('li') as HTMLElement;
    expect(within(row).getByText('This device')).toBeInTheDocument();
    const other = screen.getByText('iPad · …cd34').closest('li') as HTMLElement;
    expect(within(other).queryByText('This device')).toBeNull();
  });
});
