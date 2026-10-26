/**
 * Settings → Connections draws "Shared with Istota" when `/me` says the server
 * has it, from `GET /settings/sharing`, and asks nothing of a server without it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { User } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

await fillApiDouble(api, {
  getSettingsServices: vi.fn(async () => ({ services: [] })),
  getMailAccounts: vi.fn(async () => {
    throw Object.assign(new Error('not found'), { status: 404 });
  }),
  getSharing: vi.fn(async () => ({
    feeds: [
      {
        id: 4,
        host: 'cal.example.org',
        name: 'Team',
        state: 'active',
        last_reason: null,
        last_ok_at: null,
        created_at: null,
      },
    ],
    max_feeds: 10,
    can_add: true,
  })),
});

import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

function person(sharing: boolean | undefined): User {
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
      ...(sharing === undefined ? {} : { sharing }),
    },
    nextcloud_token: null,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Shared with Istota on Connections', () => {
  it('lists the shared calendars when the server has the card', async () => {
    render(Harness, { component: Page, user: person(true) });
    expect(await screen.findByRole('heading', { name: 'Shared with Istota' })).toBeInTheDocument();
    expect(await screen.findByText('Team')).toBeInTheDocument();
    expect(screen.queryByText(/No connected services/)).toBeNull();
  });

  it('asks nothing of a server without it', async () => {
    render(Harness, { component: Page, user: person(undefined) });
    expect(await screen.findByText(/No connected services/)).toBeInTheDocument();
    expect(api.getSharing).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: 'Shared with Istota' })).toBeNull();
  });
});
