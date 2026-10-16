import { it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import type { User } from '$lib/api';
import { get } from 'svelte/store';
import { currentNotice, clearNotices } from '$lib/stores/notices';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import Page from './+page.svelte';
import Layout from '../+layout.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';

const alice = { username: 'alice', features: { briefings: true, admin: false } } as User;
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
let saved: any;
let approvals: any[];
let caps: { author_briefings: boolean; generate_briefings: boolean };

const research = {
  scope: 'group',
  group_id: 'research',
  label: 'Research',
  member_count: 3,
  capabilities: { author_briefings: true, receive_briefings: true },
};
const other = {
  scope: 'group',
  group_id: 'other',
  label: 'Other',
  member_count: 1,
  capabilities: { author_briefings: true, receive_briefings: true },
};

function definition() {
  return {
    id: 7,
    group_id: 'research',
    name: 'news',
    title: 'News',
    cron: '0 7 * * *',
    timezone: 'Europe/Berlin',
    channel: 'email',
    enabled: false,
    revision: 1,
    excluded_user_ids: [],
    generation_mode: 'prompt',
    generation: {
      version: 1,
      prompt: 'Summarize the news',
      research: { enabled: false, prompt: '' },
      inputs: [],
    },
  };
}

function contribution(owner: string, approved = false) {
  const source = {
    id: 'notes',
    kind: 'personal_file',
    config: { owner_id: owner, path: 'notes.md' },
  };
  saved.generation.inputs = [source];
  approvals = [
    {
      source_id: 'notes',
      owner_id: owner,
      resource: { kind: source.kind, config: source.config },
      approved,
      warning: 'server wording',
      grant_revision: approved ? 1 : 0,
      definition_revision: 1,
      audience_contract: { version: 1, future_members: true, retained_history: true },
    },
  ];
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  saved = definition();
  approvals = [];
  caps = { author_briefings: true, generate_briefings: true };
  api.getBriefings.mockResolvedValue({ briefings: [], rooms: [], outputs: ['email', 'talk'] });
  api.getBrowsePresets.mockResolvedValue({ presets: [] });
  api.getFeedOptions.mockResolvedValue({ available: false, subscriptions: [], categories: [] });
  api.getBriefingConfig.mockResolvedValue({
    briefings: [],
    schedule_names: [],
    source_kinds: [],
    structured_kinds: [],
    block_templates: [],
  });
  api.getSharedBlockOptions.mockResolvedValue({ options: [] });
  api.getEmailAccounts.mockResolvedValue({ accounts: [] });
  api.getProfile.mockResolvedValue({ profile: { briefing_email_html: true } });
  api.getBriefingArchive.mockResolvedValue({ items: [], total: 0, briefing_names: [] });
  api.getGroupEditions.mockResolvedValue({ items: [], total: 0 });
  api.getBriefingTargets.mockResolvedValue({
    targets: [
      { scope: 'user', label: 'Just me', capabilities: { author_briefings: true } },
      research,
      other,
    ],
    channels: ['in_app', 'email'],
    timezone: 'Europe/Berlin',
    source_scopes: {},
  });
  api.getBriefingResources.mockResolvedValue({
    files: ['notes.md'],
    calendars: [],
    calendar_unavailable: false,
  });
  api.getGroup.mockImplementation(async (group: string) => ({
    members:
      group === 'other'
        ? [{ user_id: 'alice', display_name: 'Alice', role: 'owner' }]
        : [
            { user_id: 'alice', display_name: 'Alice', role: 'owner' },
            { user_id: 'bob', display_name: 'Bob', role: 'member' },
          ],
  }));
  api.getGroupBriefings.mockImplementation(async (group: string) => ({
    items: group === 'research' ? [clone({ ...saved, capabilities: caps })] : [],
  }));
  api.getGroupBriefing.mockImplementation(async () =>
    clone({ ...saved, capabilities: caps, source_approvals: approvals }),
  );
  api.getBriefingAudience.mockImplementation(async () => ({
    member_count: 3,
    deliverable: 1,
    excluded: 0,
    muted: 1,
    missing_destination: 1,
    sources: clone(approvals),
  }));
  api.getGroupBriefingRuns.mockResolvedValue({ items: [], total: 0 });
  api.saveGroupBriefing.mockImplementation(async (group: string, payload: any) => {
    saved = { ...clone(payload), id: 7, group_id: group, revision: payload.revision + 1 };
    return clone({ ...saved, capabilities: caps });
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// The layout reads the browser's location as well as the page store.
function visit(path: string) {
  window.history.replaceState(null, '', path);
  page.url = new URL(window.location.href);
}

async function openEditor(user = alice) {
  visit('/istota/briefings/settings/?group=research&briefing=news');
  render(Harness, { layout: Layout, component: Page, user });
  await screen.findByRole('heading', { name: 'News' });
}

it('edits a group briefing in place and saves it through the header save', async () => {
  await openEditor();
  expect(screen.getByText(/^Research · 3 members · generated once/)).toBeInTheDocument();
  await fireEvent.input(screen.getByRole('textbox', { name: /^Title/ }), {
    target: { value: 'Morning news' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(api.saveGroupBriefing).toHaveBeenCalledWith(
      'research',
      expect.objectContaining({ title: 'Morning news', revision: 1, enabled: false }),
      'news',
    ),
  );
});

it('says why it cannot run and keeps generation off until an owner approves', async () => {
  contribution('bob');
  await openEditor();
  expect(screen.getByText('Waiting for Bob')).toBeInTheDocument();
  // An author never approves another member's resource.
  expect(screen.queryByRole('button', { name: 'Review' })).toBeNull();
  expect(screen.getByText('Every shared personal source needs its owner’s approval.')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Preview' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Generate now' })).toBeDisabled();
});

it('asks the resource owner for an unchecked, explicit approval', async () => {
  contribution('alice');
  api.approveBriefingSource.mockImplementation(async () => {
    approvals[0].approved = true;
    return clone(approvals[0]);
  });
  await openEditor();
  expect(screen.getByText('Needs your approval')).toBeInTheDocument();
  await fireEvent.click(screen.getByRole('button', { name: 'Review' }));
  const dialog = await screen.findByRole('dialog', { name: 'Share your file' });
  expect(within(dialog).getByText(/Research \(3 members\), including future members/)).toBeTruthy();
  expect(within(dialog).getByText(/copies already sent cannot be recalled/)).toBeTruthy();
  const agree = within(dialog).getByRole('checkbox', { name: /I agree to share this/ });
  expect(agree).not.toBeChecked();
  expect(within(dialog).getByRole('button', { name: 'Approve' })).toBeDisabled();
  await fireEvent.click(agree);
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));
  await waitFor(() =>
    expect(api.approveBriefingSource).toHaveBeenCalledWith(
      'research',
      'news',
      expect.objectContaining({ source_id: 'notes' }),
      expect.objectContaining({ owner_id: 'alice', path: 'notes.md' }),
    ),
  );
  expect(await screen.findByText('Approved')).toBeInTheDocument();
});

it('shows a contributing member only their own sources and no editing', async () => {
  contribution('alice');
  caps = { author_briefings: false, generate_briefings: false };
  await openEditor();
  expect(screen.getByText(/Only the group's owners can change this briefing/)).toBeTruthy();
  expect(screen.queryByRole('heading', { name: 'Schedule and delivery' })).toBeNull();
  expect(screen.queryByRole('heading', { name: 'Editions' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Review' })).toBeInTheDocument();
});

it('keeps the draft and says so plainly when ownership is lost mid-edit', async () => {
  await openEditor();
  await fireEvent.input(screen.getByRole('textbox', { name: /^Title/ }), {
    target: { value: 'Kept draft' },
  });
  api.saveGroupBriefing.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  expect(await screen.findByText('You can no longer edit this briefing')).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: /^Title/ })).toHaveValue('Kept draft');
});

it('retries failed deliveries from the saved edition after a duplicate warning', async () => {
  api.getGroupBriefingRuns.mockResolvedValue({
    items: [
      {
        id: 3,
        run_id: 3,
        group_id: 'research',
        definition_id: 7,
        mode: 'publish',
        state: 'published',
        created_at: '2026-10-15T07:00:00Z',
        published_at: '2026-10-15T07:00:00Z',
      },
    ],
    total: 1,
  });
  api.getGroupBriefingDelivery.mockResolvedValue({
    counts: { sent: 1, failed: 1, uncertain: 1 },
    recipients: [],
  });
  api.retryGroupBriefingDelivery.mockResolvedValue({ retried: 2 });
  await openEditor();
  expect(await screen.findByText('1 sent · 1 failed · 1 uncertain')).toBeInTheDocument();
  await fireEvent.keyDown(screen.getByLabelText('Edition actions'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Retry failed deliveries' }));
  const confirm = await screen.findByRole('dialog', { name: 'Retry failed deliveries' });
  expect(confirm).toHaveTextContent(/could get it twice/);
  await fireEvent.click(within(confirm).getByRole('button', { name: 'Retry' }));
  await waitFor(() =>
    expect(api.retryGroupBriefingDelivery).toHaveBeenCalledWith('research', 'news', 3, true),
  );
  expect(api.runGroupBriefing).not.toHaveBeenCalled();
});

it('creates a group briefing as a disabled prompt draft from New briefing', async () => {
  visit('/istota/briefings/settings/?new=1&group=research');
  render(Harness, { layout: Layout, component: Page, user: alice });
  const audience = await screen.findByRole('radiogroup', { name: 'Who the briefing is for' });
  await waitFor(() =>
    expect(within(audience).getByRole('radio', { name: /Research/ })).toHaveAttribute(
      'aria-checked',
      'true',
    ),
  );
  await fireEvent.input(screen.getByRole('textbox', { name: /^Title/ }), {
    target: { value: 'Team news' },
  });
  await fireEvent.input(screen.getByRole('textbox', { name: /^What should it cover/ }), {
    target: { value: 'What shipped this week' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Create briefing' }));
  await waitFor(() =>
    expect(api.saveGroupBriefing).toHaveBeenCalledWith(
      'research',
      expect.objectContaining({
        name: 'team-news',
        enabled: false,
        revision: 0,
        timezone: 'Europe/Berlin',
        generation_mode: 'prompt',
        generation: expect.objectContaining({ prompt: 'What shipped this week' }),
      }),
    ),
  );
});

it('duplicates to another group as a fresh draft and lists what it leaves out', async () => {
  saved.generation_mode = 'blocks';
  saved.excluded_user_ids = ['bob'];
  saved.generation = {
    version: 1,
    contains_personal_information: true,
    audience_acknowledged: true,
    blocks: [
      {
        title: 'Notes',
        directive: null,
        render_mode: 'synthesis',
        options: {},
        sources: [{ id: 'memory', kind: 'group_memory', config: {} }],
      },
    ],
  };
  await openEditor();
  await fireEvent.keyDown(screen.getByLabelText('Briefing actions'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Duplicate to a group' }));
  const dialog = await screen.findByRole('dialog', { name: 'Duplicate to a group' });
  await fireEvent.click(within(dialog).getByRole('radio', { name: /Other/ }));
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Create copy' }));
  await waitFor(() => expect(api.saveGroupBriefing).toHaveBeenCalledTimes(1));
  const [group, payload] = api.saveGroupBriefing.mock.calls[0];
  expect(group).toBe('other');
  expect(payload).toMatchObject({
    enabled: false,
    revision: 0,
    excluded_user_ids: [],
    generation: { contains_personal_information: true, audience_acknowledged: false },
  });
  expect(payload.generation.blocks[0].sources[0].id).not.toBe('memory');
});

it('lists personal-only sources a group copy cannot keep', async () => {
  visit('/istota/briefings/settings/?name=morning');
  api.getBriefings.mockResolvedValue({
    briefings: [
      {
        id: 1,
        name: 'morning',
        cron: '0 7 * * *',
        title: '',
        output: 'email',
        enabled: true,
        managed: 'db',
      },
    ],
    rooms: [],
    outputs: ['email'],
  });
  api.getBriefingConfig.mockResolvedValue({
    briefings: [
      {
        name: 'morning',
        blocks: [
          {
            id: 1,
            briefing_name: 'morning',
            position: 0,
            title: 'Day',
            directive: '',
            render_mode: 'synthesis',
            options: {},
            sources: [
              { id: 1, position: 0, kind: 'calendar', config: {}, enabled: true },
              { id: 2, position: 1, kind: 'markets', config: {}, enabled: false },
              {
                id: 3,
                position: 2,
                kind: 'browse',
                config: { url: 'https://example.com' },
                enabled: true,
              },
              {
                id: 4,
                position: 3,
                kind: 'email',
                config: { mode: 'shared', lookback_hours: 12 },
                enabled: true,
              },
            ],
          },
        ],
      },
    ],
    schedule_names: ['morning'],
    source_kinds: ['calendar', 'markets', 'browse', 'email'],
    structured_kinds: [],
    block_templates: [],
  });
  render(Harness, { layout: Layout, component: Page, user: alice });
  await screen.findByRole('heading', { name: 'Morning Briefing' });
  await fireEvent.keyDown(screen.getByLabelText('Briefing actions'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Duplicate to a group' }));
  const dialog = await screen.findByRole('dialog', { name: 'Duplicate to a group' });
  expect(within(dialog).getByText('Day: calendar')).toBeInTheDocument();
  expect(within(dialog).getByText('Day: markets (switched off)')).toBeInTheDocument();
  expect(within(dialog).queryByText(/: email/)).not.toBeInTheDocument();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Create copy' }));
  await waitFor(() => expect(api.saveGroupBriefing).toHaveBeenCalledTimes(1));
  const payload = api.saveGroupBriefing.mock.calls[0][1];
  expect(payload.generation.blocks[0].sources.map((s: { kind: string }) => s.kind)).toEqual([
    'browse',
    'email',
  ]);
});

it('offers no switch-off for a group source in the block editor', async () => {
  saved.generation_mode = 'blocks';
  saved.generation = {
    version: 1,
    blocks: [
      {
        title: 'Markets',
        directive: null,
        render_mode: 'structured',
        options: {},
        sources: [{ id: 'm', kind: 'markets', config: {} }],
      },
    ],
  };
  await openEditor();
  await fireEvent.keyDown(screen.getByLabelText('Actions for Markets'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }));
  const editor = await screen.findByRole('dialog', { name: 'Edit block' });
  expect(within(editor).queryByLabelText('Enabled')).not.toBeInTheDocument();
  expect(within(editor).queryByRole('button', { name: 'Preview' })).not.toBeInTheDocument();
});

it('mutes group briefings from Delivery, with a group-wide row that covers each briefing', async () => {
  api.getGroupDeliveryPreferences.mockResolvedValue({ outbound_muted: false, in_app_muted: false });
  api.getGroupBriefingPreferences.mockResolvedValue({ outbound_muted: false, in_app_muted: false });
  api.saveGroupDeliveryPreferences.mockImplementation(
    async (_group: string, prefs: object) => prefs,
  );
  visit('/istota/briefings/settings/?view=delivery');
  render(Harness, { layout: Layout, component: Page, user: alice });
  const all = await screen.findByRole('checkbox', {
    name: 'Mute delivery of all Research briefings',
  });
  const one = screen.getByRole('checkbox', { name: 'Mute delivery of News' });
  expect(one).not.toBeDisabled();
  await fireEvent.click(all);
  expect(one).toBeChecked();
  expect(one).toBeDisabled();
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(api.saveGroupDeliveryPreferences).toHaveBeenCalledWith('research', {
      outbound_muted: true,
      in_app_muted: false,
    }),
  );
  // The briefing's own choice is untouched; the group row covers it.
  expect(api.saveGroupBriefingPreferences).not.toHaveBeenCalled();
});

it('shows an administrator hold, locks Enabled alone, and marks it in the sidebar', async () => {
  saved.held = { at: '2026-10-15T09:00:00Z', by_label: 'Rosa Admin' };
  // `base` is '' under vitest; the sidebar shows settings only on that path.
  visit('/briefings/settings/?group=research&briefing=news');
  render(Harness, { layout: Layout, component: Page, user: alice });
  await screen.findByRole('heading', { name: 'News' });
  expect(
    screen.getByText('Paused by an administrator', { selector: '*:not([title])' }),
  ).toBeInTheDocument();
  expect(
    screen.getByText('This briefing will not run until an administrator resumes it.'),
  ).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: /^Enabled/ })).toBeDisabled();
  expect(screen.getByRole('textbox', { name: /^Title/ })).toBeEnabled();
  expect(await screen.findByTitle('Paused by an administrator')).toBeInTheDocument();
  expect(screen.queryByTitle('Draft')).toBeNull();
});

it('reports a refused enable while held as a failed action, not lost access', async () => {
  clearNotices();
  await openEditor();
  api.saveGroupBriefing.mockRejectedValue(
    Object.assign(new Error('An administrator has paused this briefing'), { status: 409 }),
  );
  await fireEvent.click(screen.getByRole('checkbox', { name: /^Enabled/ }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(get(currentNotice)).toMatchObject({
      message: 'An administrator has paused this briefing',
      severity: 'error',
    }),
  );
  expect(screen.queryByText('You can no longer edit this briefing')).toBeNull();
  clearNotices();
});

it('lets an owner delete a disabled draft after confirming', async () => {
  api.deleteGroupBriefing.mockResolvedValue({ ok: true, deleted: true, id: 7 });
  await openEditor();
  await fireEvent.keyDown(screen.getByLabelText('Briefing actions'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
  const dialog = await screen.findByRole('dialog', { name: 'Delete briefing' });
  expect(
    within(dialog).getByText(/delete News\? It stops for every member of Research/),
  ).toBeTruthy();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(api.deleteGroupBriefing).toHaveBeenCalledWith('research', 'news', 1));
});

it('offers no delete on an enabled briefing until it is turned off', async () => {
  saved = { ...definition(), enabled: true };
  await openEditor();
  await fireEvent.keyDown(screen.getByLabelText('Briefing actions'), { key: 'Enter' });
  const item = await screen.findByRole('menuitem', { name: 'Delete (turn off first)' });
  expect(item).toHaveAttribute('aria-disabled', 'true');
  expect(screen.queryByRole('menuitem', { name: 'Delete' })).toBeNull();
});

it('offers no delete when the definition says the caller cannot author', async () => {
  caps = { author_briefings: false, generate_briefings: false };
  await openEditor();
  await fireEvent.keyDown(screen.getByLabelText('Briefing actions'), { key: 'Enter' });
  await screen.findByRole('menuitem', { name: 'Duplicate to a group' });
  expect(screen.queryByRole('menuitem', { name: /^Delete/ })).toBeNull();
});
