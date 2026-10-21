/**
 * Settings → Devices: every phone holding a credential, once each (#828).
 * A phone's row names what it can do in plain words, and one Revoke removes
 * every token it holds through one request. Location trackers that name no
 * phone, and the shared token from before per-device tokens, are listed in
 * their own card with a Remove each.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { DevicesResponse, Phone, PhoneLocationToken, User } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const store = vi.hoisted(() => ({
  devices: [] as Phone[],
  trackers: [] as PhoneLocationToken[],
  legacy: false,
}));
await fillApiDouble(api, {
  getModules: vi.fn(async () => ({ modules: [] })),
  getProfile: vi.fn(async () => ({ profile: null })),
  listDevices: vi.fn(async (): Promise<DevicesResponse> => ({
    devices: store.devices,
    location_trackers: store.trackers,
    legacy_location_token: store.legacy,
  })),
  revokePhone: vi.fn(async (deviceId: string) => {
    store.devices = store.devices.filter((d) => d.device_id !== deviceId);
    return { ok: true, token: true, location: true };
  }),
  deleteLocationDevice: vi.fn(async (id: number) => {
    store.trackers = store.trackers.filter((t) => t.id !== id);
    return { ok: true };
  }),
  deleteLegacyIngestToken: vi.fn(async () => {
    store.legacy = false;
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
const TABLET = '0E984725-C51C-4BF4-9960-E1C80E27ABA0';
const ADDED = '2026-10-01T10:00:00Z';

function locationToken(id: number, label: string, tail: string): PhoneLocationToken {
  return { id, label, token_tail: tail, created_at: ADDED, last_used_at: null };
}

function phone(
  deviceId: string,
  label: string,
  parts: { notes?: string; location?: string },
): Phone {
  return {
    device_id: deviceId,
    label,
    token: parts.notes
      ? {
          id: 1,
          scopes: ['notes'],
          token_tail: parts.notes,
          created_at: ADDED,
          last_used_at: null,
        }
      : null,
    location: parts.location ? locationToken(5, label, parts.location) : null,
    created_at: ADDED,
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
    phone(PHONE, 'iPhone', { notes: 'ab12', location: 'ef56' }),
    phone(TABLET, 'iPad', { notes: 'cd34' }),
  ];
  store.trackers = [];
  store.legacy = false;
  shellActions.deviceId = null;
  vi.clearAllMocks();
});

afterEach(() => cleanup());

describe('Settings → Devices', () => {
  it('lists a phone once with everything it can do', async () => {
    render(Harness, { component: Page, layout: SettingsLayout, user: person(true) });
    expect(await screen.findByText('iPhone · …ab12 …ef56')).toBeInTheDocument();
    expect(screen.getByText('iPad · …cd34')).toBeInTheDocument();
    expect(screen.getByText(/Quick notes, Location · added/)).toBeInTheDocument();
    expect(screen.getByText(/^Quick notes · added/)).toBeInTheDocument();
    expect(screen.queryByText('This device')).toBeNull();
    expect(screen.queryByText('Other location trackers')).toBeNull();
    expect(screen.getByRole('link', { name: 'Location settings' })).toHaveAttribute(
      'href',
      '/location/settings',
    );
  });

  it('revokes every token a phone holds in one request, after asking', async () => {
    render(Harness, { component: Page, layout: SettingsLayout, user: person(false) });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'Revoke iPhone ending …ab12 …ef56' }),
    );
    const dialog = within(await screen.findByRole('dialog'));
    expect(
      dialog.getByText(/iPhone stops saving quick notes and sending locations/),
    ).toBeInTheDocument();
    await fireEvent.click(dialog.getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(api.revokePhone).toHaveBeenCalledWith(PHONE));
    await waitFor(() => expect(screen.queryByText('iPhone · …ab12 …ef56')).toBeNull());
    expect(api.revokePhone).toHaveBeenCalledTimes(1);
    expect(screen.getByText('iPad · …cd34')).toBeInTheDocument();
    expect(api.listDevices).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('link', { name: 'Location settings' })).toBeNull();
  });

  it('lists trackers with no phone in their own card, each removable', async () => {
    store.trackers = [locationToken(8, 'Overland', 'gh78')];
    store.legacy = true;
    render(Harness, { component: Page, layout: SettingsLayout, user: person(true) });
    expect(await screen.findByText('Other location trackers')).toBeInTheDocument();
    expect(screen.getByText('Overland · …gh78')).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'Remove Overland ending gh78' }));
    const dialog = within(await screen.findByRole('dialog'));
    await fireEvent.click(dialog.getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(api.deleteLocationDevice).toHaveBeenCalledWith(8));
    await waitFor(() => expect(screen.queryByText('Overland · …gh78')).toBeNull());

    await fireEvent.click(screen.getByRole('button', { name: 'Remove the shared token' }));
    const second = within(await screen.findByRole('dialog'));
    await fireEvent.click(second.getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(api.deleteLegacyIngestToken).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText('Other location trackers')).toBeNull());
    expect(api.revokePhone).not.toHaveBeenCalled();
  });

  it('says so when there are no devices', async () => {
    store.devices = [];
    render(Harness, { component: Page, layout: SettingsLayout, user: person(false) });
    expect(await screen.findByText(/No devices yet/)).toBeInTheDocument();
  });

  it('marks the row whose device id the shell reports', async () => {
    shellActions.deviceId = PHONE;
    render(Harness, { component: Page, layout: SettingsLayout, user: person(false) });
    const row = (await screen.findByText('iPhone · …ab12 …ef56')).closest('li') as HTMLElement;
    expect(await within(row).findByText('This device')).toBeInTheDocument();
    expect(screen.getAllByText('This device')).toHaveLength(1);
  });

  it('names the voice scope, and says what revoking it stops', async () => {
    const both = phone(PHONE, 'iPhone', { notes: 'ab12' });
    both.token = { ...both.token!, scopes: ['notes', 'voice'] };
    store.devices = [both];
    render(Harness, { component: Page, layout: SettingsLayout, user: person(false) });
    expect(await screen.findByText(/Quick notes, Voice tasks · added/)).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Revoke iPhone ending …ab12' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect(
      dialog.getByText(/iPhone stops saving quick notes and sending voice tasks/),
    ).toBeInTheDocument();
  });

  it('marks the phone the page runs on as this device', async () => {
    render(DeviceTokensCard, {
      devices: { devices: store.devices, location_trackers: [], legacy_location_token: false },
      thisDeviceId: PHONE.toLowerCase(),
      onChanged: vi.fn(),
    });
    const row = screen.getByText('iPhone · …ab12 …ef56').closest('li') as HTMLElement;
    expect(within(row).getByText('This device')).toBeInTheDocument();
    const other = screen.getByText('iPad · …cd34').closest('li') as HTMLElement;
    expect(within(other).queryByText('This device')).toBeNull();
  });
});
