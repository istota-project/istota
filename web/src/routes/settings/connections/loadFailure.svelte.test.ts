/**
 * ISSUE-801: Settings → Connections when the service list fails to load. The
 * Nextcloud card renders from the signed-in identity, so with it the page still
 * has something to show and the failure sits where the list would be; without
 * it the page has nothing and the failure takes the pane. Neither shows the
 * "no connected services" empty state, which would be false.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { render, cleanup, screen, waitFor, within, fireEvent } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { User, ServiceCard } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

const karakeep: ServiceCard = {
  service: 'karakeep',
  label: 'Karakeep',
  status: 'configured',
  fields: [{ key: 'url', label: 'Server URL', type: 'url', plain: true }],
  configured_keys: ['url'],
  values: { url: 'https://keep.example.org' },
  last_updated: null,
};

const notices = vi.hoisted(() => ({ notifyError: vi.fn() }));
vi.mock('@minot/ui/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@minot/ui/stores/notices')>()),
  notifyError: notices.notifyError,
}));

await fillApiDouble(api, {
  getSettingsServices: vi.fn(async () => ({ services: [karakeep] })),
  setSecret: vi.fn(async () => ({})),
  getNextcloudSharing: vi.fn(async () => ({
    bot: 'istota',
    kinds: {
      calendars: { ceiling: 'off', available: false },
      contacts: { ceiling: 'off', available: false },
    },
    collections: [],
  })),
});

import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

function person(nextcloud: User['nextcloud_token']): User {
  return {
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
    nextcloud_token: nextcloud,
  };
}

afterEach(() => cleanup());

describe('Connections first load', () => {
  it('puts the failure where the list goes when the Nextcloud card is there', async () => {
    vi.mocked(api.getSettingsServices).mockRejectedValueOnce(new Error('Services unavailable'));
    const { container } = render(Harness, {
      component: Page,
      user: person({ connected: true, expires_at: null }),
    });

    const message = await screen.findByText('Services unavailable');
    expect(container.querySelector('.center-msg.error')).toBeNull();
    expect(screen.getByRole('heading', { name: 'Nextcloud' })).toBeInTheDocument();
    expect(screen.queryByText(/No connected services/)).toBeNull();
    expect(get(settingsSave)).toBeNull();

    const alert = message.closest('.banner') as HTMLElement;
    await fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    await screen.findByRole('heading', { name: 'Karakeep' });
    expect(screen.queryByText('Services unavailable')).toBeNull();
  });

  it('gives the failure the whole pane when there is nothing else to show', async () => {
    vi.mocked(api.getSettingsServices).mockRejectedValueOnce(new Error('Services unavailable'));
    const { container } = render(Harness, { component: Page, user: person(null) });

    const failure = await waitFor(() => {
      const el = container.querySelector('.center-msg.error');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    expect(failure.textContent).toContain('Services unavailable');
    expect(screen.queryByText(/No connected services/)).toBeNull();

    await fireEvent.click(within(failure).getByRole('button', { name: 'Retry' }));
    await screen.findByRole('heading', { name: 'Karakeep' });
  });

  it('reports a failed refresh out of band and keeps the list it has', async () => {
    const { container } = render(Harness, { component: Page, user: person(null) });
    const input = await screen.findByDisplayValue('https://keep.example.org');
    vi.mocked(api.getSettingsServices).mockRejectedValueOnce(new Error('Refresh failed'));
    await fireEvent.input(input, { target: { value: 'https://keep2.example.org' } });
    await get(settingsSave)!.save();

    await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('Refresh failed'));
    expect(container.querySelector('.banner')).toBeNull();
    expect(container.querySelector('.center-msg.error')).toBeNull();
    expect(screen.getByRole('heading', { name: 'Karakeep' })).toBeInTheDocument();
  });
});
