/**
 * ISSUE-801: what briefings settings does when part of its first load fails.
 * The schedules and the content config are the page's data: without either the
 * personal editor would render a schedule from defaults that Save then writes
 * over the stored one, so their failure takes the pane. The option lists only
 * narrow a picker, so their failure leaves the page and says what is missing.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { render, cleanup, screen, waitFor, within, fireEvent } from '@testing-library/svelte';
import type { User } from '$lib/api';
import { settingsSave } from '$lib/stores/settingsSave.svelte';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

const schedule = {
  id: 1,
  name: 'morning',
  title: 'Morning',
  cron: '30 6 * * 1-5',
  output: 'email',
  conversation_token: '',
  enabled: true,
};

await fillApiDouble(api, {
  getBriefingTargets: vi.fn(async () => ({
    targets: [],
    channels: [],
    timezone: 'UTC',
    source_scopes: {},
  })),
  getBriefings: vi.fn(async () => ({ briefings: [schedule], rooms: [], outputs: ['email'] })),
  getBrowsePresets: vi.fn(async () => ({ presets: [] })),
  getFeedOptions: vi.fn(async () => ({ available: false, subscriptions: [], categories: [] })),
  getBriefingConfig: vi.fn(async () => ({
    briefings: [{ name: 'morning', blocks: [] }],
    schedule_names: ['morning'],
    source_kinds: [],
    structured_kinds: [],
    block_templates: [],
  })),
  getBriefingPathSuggestions: vi.fn(async () => ({ paths: [] })),
  getSharedBlockOptions: vi.fn(async () => ({ options: [] })),
  getEmailAccounts: vi.fn(async () => ({ accounts: [] })),
  getProfile: vi.fn(async () => ({ profile: { briefing_email_html: false, managed: [] } })),
  upsertBriefing: vi.fn(async () => ({})),
});

vi.mock('$lib/platform/native', () => ({
  isNativeShell: vi.fn(() => false),
  shellVersion: vi.fn(() => null),
  shellAtLeast: vi.fn(() => false),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

import Page from './+page.svelte';
import { page } from '../../../../vitest-stubs/app-state.svelte';
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
    briefings: true,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: false,
  },
};

afterEach(() => {
  cleanup();
  vi.mocked(api.getBriefings).mockClear();
  vi.mocked(api.upsertBriefing).mockClear();
});

describe('briefings settings first load', () => {
  it('gives a failed schedule load the whole pane, so Save has no defaults to write', async () => {
    page.url = new URL('http://localhost/istota/briefings/settings?name=morning');
    vi.mocked(api.getBriefings).mockRejectedValueOnce(new Error('Schedules unavailable'));
    const { container } = render(Harness, { component: Page, user: person });

    const failure = await waitFor(() => {
      const el = container.querySelector('.center-msg.error');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    expect(failure.textContent).toContain('Schedules unavailable');
    expect(container.querySelector('.banner')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Schedule and delivery' })).toBeNull();
    expect(get(settingsSave)).toBeNull();

    await fireEvent.click(within(failure).getByRole('button', { name: 'Retry' }));
    await screen.findByRole('heading', { name: 'Schedule and delivery' });
    expect((screen.getByDisplayValue('30 6 * * 1-5') as HTMLInputElement).value).toBe(
      '30 6 * * 1-5',
    );
    expect(api.upsertBriefing).not.toHaveBeenCalled();
  });

  it('keeps the editor when an option list fails, and says which one', async () => {
    page.url = new URL('http://localhost/istota/briefings/settings?name=morning');
    vi.mocked(api.getBrowsePresets).mockRejectedValueOnce(new Error('down'));
    const { container } = render(Harness, { component: Page, user: person });

    await screen.findByRole('heading', { name: 'Schedule and delivery' });
    expect(container.querySelector('.center-msg.error')).toBeNull();
    const notice = await screen.findByText(/browse presets/);
    const alert = notice.closest('.banner') as HTMLElement;
    await fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByText(/browse presets/)).toBeNull());
  });

  // ISSUE-809: a failed feed list is not an empty one, so the block editor must
  // not tell the user to subscribe to a feed they may already have.
  describe('when the feed list fails', () => {
    const rssBlock = {
      id: 5,
      briefing_name: 'morning',
      position: 0,
      title: 'News',
      directive: '',
      render_mode: 'synthesis',
      options: {},
      sources: [{ id: 9, position: 0, kind: 'rss', config: {}, enabled: true }],
    };
    const rssConfig = {
      briefings: [{ name: 'morning', blocks: [rssBlock] }],
      schedule_names: ['morning'],
      source_kinds: ['rss'],
      structured_kinds: [],
      block_templates: [
        {
          id: 'news',
          label: 'Feed digest',
          description: 'Your feeds, summarized.',
          block: {
            title: 'News',
            directive: '',
            render_mode: 'synthesis',
            options: {},
            sources: [{ kind: 'rss' }],
          },
        },
      ],
    };

    function renderWithFailedFeeds(url: string) {
      vi.mocked(api.getBriefingConfig).mockResolvedValueOnce(rssConfig as never);
      vi.mocked(api.getFeedOptions).mockRejectedValueOnce(new Error('down'));
      page.url = new URL(url);
      render(Harness, { component: Page, user: person });
    }

    it('says the feeds could not be loaded in an open block editor', async () => {
      renderWithFailedFeeds('http://localhost/istota/briefings/settings?name=morning&block=5');

      await screen.findByText(/Could not load feeds/);
      await screen.findByRole('heading', { name: 'Edit block' });
      expect(screen.queryByText(/subscribe/i)).toBeNull();
      expect(screen.getByText('Feeds could not be loaded.')).toBeTruthy();
      expect(
        screen.getByText('Feeds could not be loaded, so a feed source cannot be added.'),
      ).toBeTruthy();
    });

    it('gives a feed preset the load failure as its reason', async () => {
      renderWithFailedFeeds('http://localhost/istota/briefings/settings?name=morning');

      await screen.findByText(/Could not load feeds/);
      await fireEvent.click(await screen.findByRole('button', { name: 'Add block' }));
      const preset = await screen.findByRole('button', { name: 'Feed digest' });
      expect((preset as HTMLButtonElement).disabled).toBe(true);
      expect(screen.queryByText(/subscribe/i)).toBeNull();
      expect(within(preset).getByText('Feeds could not be loaded.')).toBeTruthy();
    });
  });

  it('shows a failed profile load in the Email card instead of a guessed toggle', async () => {
    page.url = new URL('http://localhost/istota/briefings/settings?view=delivery');
    vi.mocked(api.getProfile).mockRejectedValueOnce(new Error('Profile unavailable'));
    render(Harness, { component: Page, user: person });

    const message = await screen.findByText('Profile unavailable');
    expect(screen.queryByRole('checkbox', { name: /Send as HTML/ })).toBeNull();
    expect(get(settingsSave)).toBeNull();

    const alert = message.closest('.banner') as HTMLElement;
    await fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    const toggle = await screen.findByRole('checkbox', { name: /Send as HTML/ });
    expect((toggle as HTMLInputElement).checked).toBe(false);
  });

  it('leaves Delivery alone when the schedules fail, since it reads none', async () => {
    page.url = new URL('http://localhost/istota/briefings/settings?view=delivery');
    vi.mocked(api.getBriefings).mockRejectedValueOnce(new Error('Schedules unavailable'));
    const { container } = render(Harness, { component: Page, user: person });

    await screen.findByRole('checkbox', { name: /Send as HTML/ });
    expect(container.querySelector('.center-msg.error')).toBeNull();
    expect(screen.queryByText('Schedules unavailable')).toBeNull();
  });
});
