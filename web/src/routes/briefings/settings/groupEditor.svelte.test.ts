import { it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import type { User } from '$lib/api';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
  getBriefings: vi.fn(async () => ({ briefings: [], rooms: [], outputs: ['email'] })),
  getBrowsePresets: vi.fn(async () => ({ presets: [] })),
  getFeedOptions: vi.fn(async () => ({ available: false, subscriptions: [], categories: [] })),
  getBriefingConfig: vi.fn(async () => ({
    briefings: [],
    schedule_names: [],
    source_kinds: [],
    structured_kinds: [],
  })),
  getBriefingPathSuggestions: vi.fn(async () => ({ paths: [] })),
  getSharedBlockOptions: vi.fn(async () => ({ options: [] })),
  getProfile: vi.fn(async () => ({ profile: { briefing_email_html: true } })),
  getBriefingTargets: vi.fn(async () => ({
    targets: [
      { scope: 'user', label: 'Just me', capabilities: { author_briefings: true } },
      {
        scope: 'group',
        group_id: 'research',
        label: 'Research',
        member_count: 3,
        capabilities: { author_briefings: true },
      },
    ],
    channels: ['in_app', 'email'],
    timezone: 'Europe/Berlin',
    source_scopes: {
      personal_file: 'Personal contribution',
      personal_calendar: 'Personal contribution',
      group_memory: 'Current group',
      markets: 'Public',
    },
  })),
  getBriefingResources: vi.fn(async () => ({
    files: ['notes.md'],
    calendars: [{ value: 'https://calendar.example.com/alice/work/', label: 'My work calendar' }],
    calendar_unavailable: false,
  })),
  getGroupBriefings: vi.fn(async () => ({ items: [] })),
});
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
const person = { username: 'alice', features: { briefings: true, admin: false } } as User;
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it('starts at Just me and keeps the group audience visible beside the editor', async () => {
  render(Harness, { component: Page, user: person });
  await fireEvent.click(await screen.findByRole('button', { name: 'New briefing' }));
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Distribution target' })).toHaveTextContent(
      'Just me',
    ),
  );
  await pick('Distribution target', 'Research (3 members)');
  expect(screen.getByText(/Generated once per run/)).toHaveTextContent('Research');
  expect(screen.getByLabelText(/^Timezone/)).toHaveValue('Europe/Berlin');
  expect(screen.getByLabelText(/^Generation prompt/)).toBeInTheDocument();
  expect(screen.queryByLabelText('Conversation token')).not.toBeInTheDocument();
});

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
let saved: any;
let approvals: any[];
const caps = { author_briefings: true, generate_briefings: true };
const makeDefinition = () => ({
  id: 7,
  group_id: 'research',
  name: 'news',
  title: 'News',
  cron: '0 7 * * *',
  timezone: 'Europe/Berlin',
  channel: 'email',
  enabled: false,
  revision: 1,
  generation_mode: 'prompt',
  generation: {
    version: 1,
    prompt: 'Summarize the news',
    research: { enabled: false, prompt: '' },
    inputs: [],
  },
  capabilities: caps,
  source_approvals: [],
});
beforeEach(() => {
  vi.clearAllMocks();
  page.url = new URL('http://localhost/istota/briefings/settings');
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  saved = makeDefinition();
  approvals = [];
  api.getGroup.mockResolvedValue({
    members: [
      { user_id: 'alice', display_name: 'Alice', role: 'owner' },
      { user_id: 'bob', display_name: 'Bob', role: 'member' },
    ],
  });
  api.getGroupBriefings.mockImplementation(async () => ({ items: [] }));
  api.getGroupBriefing.mockImplementation(async () =>
    clone({ ...saved, source_approvals: approvals }),
  );
  api.getBriefingAudience.mockImplementation(async () =>
    clone({
      member_count: 3,
      deliverable: 1,
      excluded: 0,
      muted: 1,
      missing_destination: 1,
      sources: approvals,
    }),
  );
  api.saveGroupBriefing.mockImplementation(async (group, payload) => {
    saved = {
      ...clone(payload),
      id: 7,
      group_id: group,
      revision: payload.revision + 1,
      capabilities: caps,
    };
    return clone(saved);
  });
  api.approveBriefingSource.mockImplementation(async () => {
    approvals[0].approved = true;
    return clone(approvals[0]);
  });
  api.revokeBriefingSource.mockImplementation(async () => {
    approvals[0].approved = false;
    return clone(approvals[0]);
  });
  api.runGroupBriefing.mockResolvedValue({
    id: 19,
    run_id: 19,
    group_id: 'research',
    definition_id: 7,
    mode: 'preview',
    state: 'preview_ready',
    subject: 'Draft news',
    body_md: 'Private preview body',
  });
});
async function pick(label: string, option: string) {
  const trigger = screen.getByRole('button', { name: label, exact: true });
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const item = await screen.findByRole('option', { name: option, exact: true });
  await fireEvent.pointerUp(item, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(item);
  await waitFor(() => expect(trigger).toHaveAttribute('aria-expanded', 'false'));
}
async function newGroup() {
  render(Harness, { component: Page, user: person });
  await fireEvent.click(await screen.findByRole('button', { name: 'New briefing' }));
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Distribution target' })).toHaveTextContent(
      'Just me',
    ),
  );
  await pick('Distribution target', 'Research (3 members)');
  await fireEvent.input(screen.getByLabelText('Name', { exact: true }), {
    target: { value: 'news' },
  });
  await fireEvent.input(screen.getByLabelText(/^Generation prompt/), {
    target: { value: 'Summarize the news' },
  });
}
async function existingGroup() {
  api.getGroupBriefings.mockImplementation(async (group) => ({
    items: group === saved.group_id ? [clone({ ...saved, source_approvals: approvals })] : [],
  }));
  render(Harness, { component: Page, user: person });
  await fireEvent.click(await screen.findByRole('button', { name: 'Edit news' }));
  await screen.findByRole('button', { name: 'Preview', exact: true });
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Reload saved briefing' })).not.toBeDisabled(),
  );
}
function personalSource(owner = 'alice') {
  const source = {
    id: 'selected',
    kind: 'personal_file',
    config: { owner_id: owner, path: 'notes.md' },
  };
  saved.generation.inputs = [source];
  approvals = [
    {
      source_id: 'selected',
      owner_id: owner,
      resource: { kind: source.kind, config: source.config },
      approved: false,
      warning: `${owner}'s notes.md is used each run for Research via Email (3 members). Future members can read retained editions. Copies cannot be recalled.`,
      grant_revision: 0,
      definition_revision: 1,
      audience_contract: {
        version: 1,
        group_id: 'research',
        future_members: true,
        retained_history: true,
        recurring_use: true,
        copies_cannot_recall: true,
      },
    },
  ];
}
it('saves a disabled prompt draft, reviews recipient counts and previews without publishing', async () => {
  await newGroup();
  await pick('Delivery channel', 'Email');
  await fireEvent.click(screen.getByLabelText('Public research', { exact: true }));
  expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
  await fireEvent.input(screen.getByLabelText(/^Public research prompt/), {
    target: { value: 'Find public news' },
  });
  expect(screen.queryByText(/domain allowlist/i)).not.toBeInTheDocument();
  await fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Preview', exact: true })).not.toBeDisabled(),
  );
  expect(api.saveGroupBriefing).toHaveBeenCalledWith(
    'research',
    expect.objectContaining({
      enabled: false,
      revision: 0,
      channel: 'email',
      generation_mode: 'prompt',
      generation: expect.objectContaining({
        prompt: 'Summarize the news',
        research: { enabled: true, prompt: 'Find public news' },
      }),
    }),
    undefined,
  );
  expect(screen.getByText(/1 deliverable/)).toHaveTextContent('1 muted · 1 missing destinations');
  expect(screen.getByRole('button', { name: 'Distribution target' })).toBeDisabled();
  await fireEvent.click(screen.getByRole('button', { name: 'Preview', exact: true }));
  await screen.findByText('Private preview body');
  expect(api.runGroupBriefing).toHaveBeenCalledWith(
    'research',
    'news',
    'preview',
    expect.any(String),
  );
  expect(api.runGroupBriefing).toHaveBeenCalledTimes(1);
  await fireEvent.click(screen.getByRole('button', { name: 'Enable schedule' }));
  await waitFor(() =>
    expect(api.saveGroupBriefing).toHaveBeenLastCalledWith(
      'research',
      expect.objectContaining({ enabled: true, revision: 1 }),
      'news',
    ),
  );
});
it('requires unchecked source consent and renews it after a channel change', async () => {
  personalSource();
  await existingGroup();
  expect(screen.getByText('Personal contribution: personal_file · alice · notes.md')).toBeVisible();
  const checkbox = screen.getByRole('checkbox', {
    name: /I approve recurring sharing of selected/,
  });
  expect(checkbox).not.toBeChecked();
  expect(screen.getByRole('button', { name: 'Approve source' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Preview', exact: true })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Enable schedule' })).toBeDisabled();
  await fireEvent.click(checkbox);
  await fireEvent.click(screen.getByRole('button', { name: 'Approve source' }));
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Preview', exact: true })).not.toBeDisabled(),
  );
  expect(api.approveBriefingSource).toHaveBeenCalledWith(
    'research',
    'news',
    expect.objectContaining({ source_id: 'selected', definition_revision: 1, grant_revision: 0 }),
    undefined,
  );
  expect(screen.getByText(/alice's notes.md is used each run/)).toBeVisible();
  await pick('Delivery channel', 'In app');
  expect(screen.getByRole('button', { name: 'Enable schedule' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Preview', exact: true })).toBeDisabled();
});
it('never lets an author approve another member’s contribution', async () => {
  personalSource('bob');
  await existingGroup();
  expect(screen.getByText('Only bob can approve or revoke this source.')).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Approve source' })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Personal file')).not.toBeInTheDocument();
});
it('keeps the audience outside collapsed content on a narrow screen and duplicates disabled', async () => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 375 });
  await existingGroup();
  await fireEvent.keyDown(screen.getByLabelText('Actions for Selected inputs'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }));
  await fireEvent.click(
    within(await screen.findByRole('dialog', { name: 'Edit block' })).getByRole('button', {
      name: 'Cancel',
      exact: true,
    }),
  );
  expect(screen.getByText(/Generated once per run/)).toBeVisible();
  await fireEvent.click(screen.getByRole('button', { name: 'Duplicate for group' }));
  expect(screen.getByRole('button', { name: 'Distribution target' })).not.toBeDisabled();
  expect(screen.getByRole('button', { name: 'Preview', exact: true })).toBeDisabled();
  await fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() =>
    expect(api.saveGroupBriefing).toHaveBeenCalledWith(
      'research',
      expect.objectContaining({
        enabled: false,
        revision: 0,
        name: expect.stringMatching(/^news-copy-/),
      }),
      undefined,
    ),
  );
});
it('keeps a conflicted draft and removes management after an ownership refusal', async () => {
  await existingGroup();
  await fireEvent.input(screen.getByLabelText(/^Generation prompt/), {
    target: { value: 'My unsaved prompt' },
  });
  api.saveGroupBriefing.mockRejectedValueOnce(
    Object.assign(new Error('revision_conflict'), { status: 409 }),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await screen.findByText('revision_conflict');
  expect(screen.getByLabelText(/^Generation prompt/)).toHaveValue('My unsaved prompt');
  api.saveGroupBriefing.mockRejectedValueOnce(
    Object.assign(new Error('briefing_forbidden'), { status: 403 }),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await screen.findByText('briefing_forbidden');
  expect(screen.queryByRole('button', { name: 'Save draft' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Preview', exact: true })).not.toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: 'Excluded users' })).not.toBeInTheDocument();
});

it('reuses ordered block editing and preserves saved source identities', async () => {
  saved.generation_mode = 'blocks';
  saved.generation = {
    version: 1,
    blocks: [
      {
        title: 'Notebook',
        directive: '',
        render_mode: 'structured',
        options: {},
        sources: [{ id: 'notebook-source', kind: 'group_memory', config: {} }],
      },
      {
        title: 'Markets',
        directive: '',
        render_mode: 'structured',
        options: {},
        sources: [{ id: 'market-source', kind: 'markets', config: {} }],
      },
    ],
  };
  await existingGroup();
  await fireEvent.keyDown(screen.getByLabelText('Actions for Markets'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Move up' }));
  await fireEvent.keyDown(screen.getByLabelText('Actions for Markets'), { key: 'Enter' });
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }));
  const editor = await screen.findByRole('dialog', { name: 'Edit block' });
  expect(within(editor).queryByRole('button', { name: 'Preview' })).not.toBeInTheDocument();
  await fireEvent.input(within(editor).getByLabelText('Title'), {
    target: { value: 'Market update' },
  });
  await fireEvent.click(within(editor).getByRole('button', { name: 'Save block' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  await fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() => expect(api.saveGroupBriefing).toHaveBeenCalled());
  const payload = api.saveGroupBriefing.mock.calls[0][1];
  expect(payload.generation.blocks.map((b: any) => [b.title, b.sources[0].id])).toEqual([
    ['Market update', 'market-source'],
    ['Notebook', 'notebook-source'],
  ]);
  expect(api.putBriefingBlock).not.toHaveBeenCalled();
  expect(api.putBriefingSource).not.toHaveBeenCalled();
});
it('lets a member select and approve only their own requested calendar', async () => {
  saved.capabilities = { author_briefings: false, generate_briefings: false };
  delete saved.generation;
  approvals = [
    {
      source_id: 'calendar',
      owner_id: 'alice',
      resource: { kind: 'personal_calendar', config: { owner_id: 'alice' } },
      approved: false,
      warning:
        'This calendar is shared every run with current and future Research members via Email. Retained editions remain readable; copies cannot be recalled.',
      definition_revision: 1,
      grant_revision: 0,
      audience_contract: { version: 1, group_id: 'research' },
    },
  ];
  api.getGroupBriefings.mockImplementation(async (group) => ({
    items: group === saved.group_id ? [clone({ ...saved, source_approvals: approvals })] : [],
  }));
  render(Harness, { component: Page, user: person });
  await fireEvent.click(await screen.findByRole('button', { name: 'Review sources for news' }));
  await screen.findByRole('button', { name: 'Personal calendar' });
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Approve source' })).toBeDisabled(),
  );
  await pick('Personal calendar', 'My work calendar');
  expect(screen.getByRole('checkbox', { name: 'time', exact: true })).toBeChecked();
  expect(screen.getByRole('checkbox', { name: 'title', exact: true })).toBeChecked();
  expect(screen.getByRole('checkbox', { name: /Include private-marked/ })).not.toBeChecked();
  const consent = screen.getByRole('checkbox', { name: /I approve recurring sharing/ });
  expect(consent).not.toBeChecked();
  await fireEvent.click(consent);
  await pick('Days ahead', '7');
  expect(consent).not.toBeChecked();
  await fireEvent.click(consent);
  await fireEvent.click(screen.getByRole('button', { name: 'Approve source' }));
  await waitFor(() =>
    expect(api.approveBriefingSource).toHaveBeenCalledWith(
      'research',
      'news',
      expect.objectContaining({ source_id: 'calendar' }),
      {
        owner_id: 'alice',
        calendar_url: 'https://calendar.example.com/alice/work/',
        fields: ['time', 'title'],
        include_private: false,
        lookback_days: 0,
        lookahead_days: 7,
      },
    ),
  );
  expect(api.saveGroupBriefing).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Save draft' })).not.toBeInTheDocument();
});

it('preselects the All users group link and explains future installation membership', async () => {
  page.url = new URL('http://localhost/istota/briefings/settings?new=1&group=everyone');
  api.getBriefingTargets.mockResolvedValueOnce({
    targets: [
      {
        scope: 'group',
        group_id: 'everyone',
        label: 'All users',
        system_key: 'all_users',
        member_count: 4,
        capabilities: caps,
      },
    ],
    channels: ['in_app'],
    timezone: 'UTC',
    source_scopes: {},
  });
  render(Harness, {
    component: Page,
    user: { ...person, features: { ...person.features, admin: true } },
  });
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Distribution target' })).toHaveTextContent(
      'All users (4 members)',
    ),
  );
  expect(
    screen.getByText(/Everyone currently using this installation and anyone who joins later/),
  ).toBeVisible();
  expect(screen.getByLabelText(/^Timezone/)).toHaveValue('UTC');
  expect(screen.getByRole('button', { name: 'Preview', exact: true })).toBeDisabled();
});

it('selects current members with the shared multiselect and preserves absent selections', async () => {
  api.getGroup.mockResolvedValue({
    members: [
      { user_id: 'alice', display_name: 'Alice', role: 'owner' },
      { user_id: 'bob', display_name: 'Bob', role: 'member' },
    ],
  });
  saved.excluded_user_ids = ['departed'];
  await existingGroup();
  expect(
    screen.getByText(/Excluded members receive no deliveries or in-app notifications/),
  ).toBeVisible();
  expect(screen.getByRole('button', { name: 'Remove departed' })).toBeVisible();
  const search = screen.getByRole('combobox', { name: 'Excluded users' });
  await fireEvent.input(search, { target: { value: 'Bob' } });
  await fireEvent.keyDown(search, { key: 'ArrowDown' });
  await fireEvent.keyDown(search, { key: 'Enter' });
  await screen.findByRole('button', { name: 'Remove Bob' });
  await fireEvent.keyDown(search, { key: 'Escape' });
  await fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() =>
    expect(api.saveGroupBriefing).toHaveBeenLastCalledWith(
      'research',
      expect.objectContaining({ excluded_user_ids: ['departed', 'bob'], revision: 1 }),
      'news',
    ),
  );
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Save draft' })).not.toBeDisabled(),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Remove departed' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Remove Bob' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() =>
    expect(api.saveGroupBriefing).toHaveBeenLastCalledWith(
      'research',
      expect.objectContaining({ excluded_user_ids: [], revision: 2 }),
      'news',
    ),
  );
});

it('filters duplicated exclusions to the new group', async () => {
  saved.excluded_user_ids = ['bob', 'departed'];
  const discovery = await api.getBriefingTargets();
  api.getBriefingTargets.mockResolvedValueOnce({
    ...discovery,
    targets: [
      ...discovery.targets,
      {
        scope: 'group',
        group_id: 'other',
        label: 'Other',
        member_count: 1,
        capabilities: caps,
      },
    ],
  });
  api.getGroup.mockImplementation(async (group) => ({
    members:
      group === 'other'
        ? [{ user_id: 'alice', display_name: 'Alice', role: 'owner' }]
        : [
            { user_id: 'alice', display_name: 'Alice', role: 'owner' },
            { user_id: 'bob', display_name: 'Bob', role: 'member' },
          ],
  }));
  await existingGroup();
  await fireEvent.click(screen.getByRole('button', { name: 'Duplicate for group' }));
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Distribution target' })).not.toBeDisabled(),
  );
  expect(screen.queryByRole('button', { name: 'Remove departed' })).toBeNull();
  await pick('Distribution target', 'Other (1 members)');
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Save draft' })).not.toBeDisabled(),
  );
  expect(screen.queryByRole('button', { name: 'Remove Bob' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() =>
    expect(api.saveGroupBriefing).toHaveBeenLastCalledWith(
      'other',
      expect.objectContaining({ excluded_user_ids: [], revision: 0 }),
      undefined,
    ),
  );
});
