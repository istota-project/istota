/**
 * The Nextcloud card lists the user's calendars and address books at the
 * bot's current level, read back from Nextcloud. The ceiling bounds raising
 * only, so a share above it or in a kind the bot cannot use must still show at
 * its real level and stay removable.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { NextcloudSharing, NextcloudTokenStatus } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import NextcloudCard from './NextcloudCard.svelte';

const CONNECTED: NextcloudTokenStatus = { connected: true, expires_at: null };

function sharing(over: Partial<NextcloudSharing> = {}): NextcloudSharing {
  return {
    bot: 'istota',
    kinds: {
      calendars: { ceiling: 'full', available: true },
      contacts: { ceiling: 'full', available: true },
    },
    collections: [
      { id: 'cal-personal', kind: 'calendars', name: 'Personal', level: 'off' },
      { id: 'cal-work', kind: 'calendars', name: 'Work', level: 'off' },
      { id: 'book-contacts', kind: 'contacts', name: 'Contacts', level: 'off' },
    ],
    ...over,
  };
}

beforeEach(() => {
  api.getNextcloudSharing.mockReset();
  api.saveNextcloudSharing.mockReset();
  api.disconnectNextcloudToken.mockReset();
});

afterEach(() => {
  cleanup();
  inShell(null);
});

/** The page as the given iOS app version sees it: its User-Agent token and its connection plugin. */
function inShell(version: string | null) {
  const ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15';
  Object.defineProperty(window.navigator, 'userAgent', {
    value: version ? `${ua} IstotaApp/${version}` : ua,
    configurable: true,
  });
  (window as { Capacitor?: unknown }).Capacitor = version
    ? { Plugins: { IstotaConnection: { openSettings: vi.fn() } } }
    : undefined;
}

async function mount(
  s: NextcloudSharing | Error,
  props: { token?: NextcloudTokenStatus; username?: string; onDisconnected?: () => void } = {},
) {
  if (s instanceof Error) api.getNextcloudSharing.mockRejectedValue(s);
  else api.getNextcloudSharing.mockResolvedValue(s);
  render(NextcloudCard, { token: CONNECTED, username: 'alice', ...props });
  await waitFor(() => expect(api.getNextcloudSharing).toHaveBeenCalled());
}

async function openSelect(label: string) {
  const trigger = await screen.findByLabelText(label);
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  return trigger;
}

async function pickLevel(label: string, optionLabel: string) {
  await openSelect(label);
  const option = await screen.findByRole('option', { name: optionLabel });
  await fireEvent.pointerUp(option, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(option);
}

async function headerSave() {
  const aggregate = get(settingsSave);
  expect(aggregate).not.toBeNull();
  await aggregate!.save();
}

describe('NextcloudCard', () => {
  it('renders a row per collection under its group', async () => {
    await mount(sharing());
    const calendars = await screen.findByRole('group', { name: 'Calendars' });
    expect(within(calendars).getByLabelText('Personal access level')).toBeInTheDocument();
    expect(within(calendars).getByLabelText('Work access level')).toBeInTheDocument();
    const books = screen.getByRole('group', { name: 'Address books' });
    expect(within(books).getByLabelText('Contacts access level')).toBeInTheDocument();
    expect(screen.getByText(/through its own Nextcloud account \(istota\)/)).toBeInTheDocument();
  });

  it('offers levels only up to the ceiling', async () => {
    await mount(
      sharing({
        kinds: {
          calendars: { ceiling: 'readonly', available: true },
          contacts: { ceiling: 'full', available: true },
        },
      }),
    );
    await openSelect('Personal access level');
    expect(await screen.findByRole('option', { name: 'Read-only' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Read and write' })).not.toBeInTheDocument();
  });

  it('shows a share above the ceiling at its real level, as a disabled option', async () => {
    await mount(
      sharing({
        kinds: {
          calendars: { ceiling: 'readonly', available: true },
          contacts: { ceiling: 'full', available: true },
        },
        collections: [{ id: 'cal-personal', kind: 'calendars', name: 'Personal', level: 'full' }],
      }),
    );
    const trigger = await screen.findByLabelText('Personal access level');
    expect(trigger).toHaveTextContent('Read and write');
    expect(screen.getByText("Above this instance's limit.")).toBeInTheDocument();
    await openSelect('Personal access level');
    const above = await screen.findByRole('option', { name: 'Read and write' });
    expect(above).toHaveAttribute('data-disabled');
    expect(screen.getByRole('option', { name: 'Read-only' })).not.toHaveAttribute('data-disabled');
  });

  it('shows only the shared rows of an unavailable kind, each removable', async () => {
    await mount(
      sharing({
        kinds: {
          calendars: { ceiling: 'full', available: false },
          contacts: { ceiling: 'full', available: true },
        },
        collections: [
          { id: 'cal-personal', kind: 'calendars', name: 'Personal', level: 'readonly' },
          { id: 'cal-work', kind: 'calendars', name: 'Work', level: 'off' },
          { id: 'book-contacts', kind: 'contacts', name: 'Contacts', level: 'off' },
        ],
      }),
    );
    expect(await screen.findByText('Istota does not use these on this instance.')).toBeVisible();
    expect(screen.queryByLabelText('Work access level')).not.toBeInTheDocument();
    await openSelect('Personal access level');
    expect(await screen.findByRole('option', { name: 'No access' })).not.toHaveAttribute(
      'data-disabled',
    );
    expect(screen.getByRole('option', { name: 'Read-only' })).toHaveAttribute('data-disabled');
    expect(screen.queryByRole('option', { name: 'Read and write' })).not.toBeInTheDocument();
  });

  it('hides a kind the instance caps at off when nothing in it is shared', async () => {
    await mount(
      sharing({
        kinds: {
          calendars: { ceiling: 'off', available: true },
          contacts: { ceiling: 'full', available: true },
        },
      }),
    );
    await screen.findByRole('group', { name: 'Address books' });
    expect(screen.queryByRole('group', { name: 'Calendars' })).not.toBeInTheDocument();
  });

  it('says so when an offered kind has no collections', async () => {
    await mount(sharing({ collections: [] }));
    expect(await screen.findByText('You have no calendars in Nextcloud.')).toBeInTheDocument();
    expect(screen.getByText('You have no address books in Nextcloud.')).toBeInTheDocument();
  });

  it('shows no rows for the bot account itself', async () => {
    await mount(sharing(), { username: 'Istota' });
    expect(await screen.findByText(/Istota's own Nextcloud account/)).toBeInTheDocument();
    expect(screen.queryByRole('group')).not.toBeInTheDocument();
  });

  it('shows a listing failure and keeps Reconnect', async () => {
    await mount(new Error('Nextcloud is not connected. Reconnect in Settings.'));
    expect(
      await screen.findByText('Nextcloud is not connected. Reconnect in Settings.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument();
  });

  it('lists nothing and explains lingering shares when not connected', async () => {
    render(NextcloudCard, { token: { connected: false, expires_at: null } });
    expect(await screen.findByText('Not connected')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect' })).toBeInTheDocument();
    expect(screen.getByText(/stay shared until you remove them in Nextcloud/)).toBeInTheDocument();
    expect(api.getNextcloudSharing).not.toHaveBeenCalled();
  });

  describe('saving', () => {
    it('is not dirty until a level changes', async () => {
      await mount(sharing());
      await screen.findByLabelText('Personal access level');
      expect(get(settingsSave)?.dirty).toBe(false);
    });

    it('sends only the rows whose level changed', async () => {
      await mount(sharing());
      api.saveNextcloudSharing.mockResolvedValue({
        ...sharing({
          collections: [
            { id: 'cal-personal', kind: 'calendars', name: 'Personal', level: 'full' },
            { id: 'cal-work', kind: 'calendars', name: 'Work', level: 'off' },
            { id: 'book-contacts', kind: 'contacts', name: 'Contacts', level: 'readonly' },
          ],
        }),
        failed: [],
      });
      await pickLevel('Personal access level', 'Read and write');
      await pickLevel('Contacts access level', 'Read-only');
      await waitFor(() => expect(get(settingsSave)?.dirty).toBe(true));
      await headerSave();
      expect(api.saveNextcloudSharing).toHaveBeenCalledWith({
        'cal-personal': 'full',
        'book-contacts': 'readonly',
      });
      expect(await screen.findByText('Saved.')).toBeInTheDocument();
      expect(get(settingsSave)?.dirty).toBe(false);
    });

    it('shows a partial failure under its own row', async () => {
      await mount(sharing());
      api.saveNextcloudSharing.mockResolvedValue({
        ...sharing({
          collections: [
            { id: 'cal-personal', kind: 'calendars', name: 'Personal', level: 'full' },
            { id: 'cal-work', kind: 'calendars', name: 'Work', level: 'off' },
            { id: 'book-contacts', kind: 'contacts', name: 'Contacts', level: 'off' },
          ],
        }),
        failed: [{ id: 'cal-work', name: 'Work', reason: 'Nextcloud did not apply this change' }],
      });
      await pickLevel('Personal access level', 'Read and write');
      await pickLevel('Work access level', 'Read-only');
      await headerSave();
      expect(await screen.findByText('Nextcloud did not apply this change')).toBeInTheDocument();
      expect(screen.getByText('Some changes were not applied.')).toBeInTheDocument();
      expect(screen.getByLabelText('Work access level')).toHaveTextContent('No access');
    });

    it('reports a refused save against the card', async () => {
      await mount(sharing());
      api.saveNextcloudSharing.mockRejectedValue(
        new Error('Nextcloud is not reachable right now.'),
      );
      await pickLevel('Personal access level', 'Read-only');
      await headerSave();
      expect(await screen.findByText('Nextcloud is not reachable right now.')).toBeInTheDocument();
    });
  });

  describe('disconnect', () => {
    const SHARED = sharing({
      collections: [{ id: 'cal-personal', kind: 'calendars', name: 'Personal', level: 'readonly' }],
    });

    it('disconnects without asking when nothing is shared', async () => {
      const onDisconnected = vi.fn();
      await mount(sharing(), { onDisconnected });
      api.disconnectNextcloudToken.mockResolvedValue({ ok: true, was_connected: true });
      await screen.findByLabelText('Personal access level');
      await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
      await waitFor(() => expect(api.disconnectNextcloudToken).toHaveBeenCalledWith(false));
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(onDisconnected).toHaveBeenCalled();
    });

    it('asks when something is shared, and unshares on request', async () => {
      const onDisconnected = vi.fn();
      await mount(SHARED, { onDisconnected });
      api.disconnectNextcloudToken.mockResolvedValue({ ok: true, was_connected: true });
      await screen.findByLabelText('Personal access level');
      await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
      const dialog = await screen.findByRole('dialog');
      await fireEvent.click(
        within(dialog).getByRole('button', { name: 'Disconnect and stop sharing' }),
      );
      await waitFor(() => expect(api.disconnectNextcloudToken).toHaveBeenCalledWith(true));
      expect(onDisconnected).toHaveBeenCalled();
    });

    it('keeps sharing when asked to', async () => {
      await mount(SHARED);
      api.disconnectNextcloudToken.mockResolvedValue({ ok: true, was_connected: true });
      await screen.findByLabelText('Personal access level');
      await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
      const dialog = await screen.findByRole('dialog');
      await fireEvent.click(
        within(dialog).getByRole('button', { name: 'Disconnect, keep sharing' }),
      );
      await waitFor(() => expect(api.disconnectNextcloudToken).toHaveBeenCalledWith(false));
    });

    it('asks when the listing failed, since the card cannot know', async () => {
      await mount(new Error('Nextcloud is not reachable right now.'));
      await screen.findByText('Nextcloud is not reachable right now.');
      await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByText(/could not check/)).toBeInTheDocument();
      expect(api.disconnectNextcloudToken).not.toHaveBeenCalled();
    });

    it('names what is still shared and keeps the connection', async () => {
      const onDisconnected = vi.fn();
      await mount(SHARED, { onDisconnected });
      api.disconnectNextcloudToken.mockRejectedValue(
        Object.assign(new Error('Could not stop sharing.'), {
          status: 409,
          body: { error: 'Could not stop sharing.', still_shared: ['Personal'] },
        }),
      );
      await screen.findByLabelText('Personal access level');
      await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
      const dialog = await screen.findByRole('dialog');
      await fireEvent.click(
        within(dialog).getByRole('button', { name: 'Disconnect and stop sharing' }),
      );
      expect(
        await within(dialog).findByText(
          'Could not stop sharing: Personal. Your Nextcloud connection was kept so you can try again.',
        ),
      ).toBeInTheDocument();
      expect(onDisconnected).not.toHaveBeenCalled();
    });
  });
});

describe('the connect route in the iOS app', () => {
  const NOTE = 'Opens in the browser. You may need to sign in to Istota there first.';

  it('opens Reconnect and Connect in the browser from the 0.12.0 app', async () => {
    inShell('0.12.0');
    await mount(sharing());
    const reconnect = await screen.findByRole('link', { name: 'Reconnect' });
    expect(reconnect.getAttribute('href')).toMatch(/\/reconnect$/);
    expect(reconnect).toHaveAttribute('target', '_blank');
    expect(screen.getByText(NOTE)).toBeInTheDocument();
    cleanup();
    render(NextcloudCard, { token: { connected: false, expires_at: null }, username: 'alice' });
    const connect = await screen.findByRole('link', { name: 'Connect' });
    expect(connect).toHaveAttribute('target', '_blank');
    expect(screen.getByText(NOTE)).toBeInTheDocument();
  });

  it('keeps the page navigation in a browser and in older apps', async () => {
    for (const version of [null, '0.11.0']) {
      inShell(version);
      await mount(sharing());
      expect(await screen.findByRole('button', { name: 'Reconnect' })).toBeInTheDocument();
      expect(screen.queryByRole('link', { name: 'Reconnect' })).toBeNull();
      expect(screen.queryByText(NOTE)).toBeNull();
      cleanup();
    }
  });
});
