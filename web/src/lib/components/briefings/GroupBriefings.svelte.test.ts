import { it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { __history } from '../../../../vitest-stubs/app-navigation';
import GroupBriefings from './GroupBriefings.svelte';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
const caps = { author_briefings: true, generate_briefings: true, read_briefings: true };
let definition: any;
let run: any;
beforeEach(() => {
  vi.clearAllMocks();
  __history.reset('/groups/?group=team');
  definition = {
    id: 7,
    group_id: 'team',
    name: 'news',
    title: 'Team news',
    channel: 'email',
    cron: '0 7 * * *',
    timezone: 'UTC',
    enabled: true,
    capabilities: { ...caps },
    next_run: '2026-10-16T07:00:00Z',
    last_run: { id: 3, state: 'published' },
  };
  run = {
    id: 3,
    run_id: 3,
    group_id: 'team',
    definition_id: 7,
    state: 'published',
    mode: 'publish',
    subject: 'Common edition',
    body_md: 'Retained group content',
    published_at: '2026-10-15T07:00:00Z',
  };
  api.getGroupBriefings.mockImplementation(async () => ({ items: [definition] }));
  api.getGroupBriefing.mockImplementation(async () => definition);
  api.getGroupBriefingRuns.mockImplementation(async () => ({ items: [run], total: 1 }));
  api.getGroupBriefingRun.mockImplementation(async () => run);
  api.getGroupDeliveryPreferences.mockResolvedValue({ outbound_muted: false, in_app_muted: false });
  api.getGroupBriefingPreferences.mockResolvedValue({ outbound_muted: false, in_app_muted: false });
  api.saveGroupBriefingPreferences.mockImplementation(async (_g, _n, prefs) => prefs);
  api.getGroupBriefingDelivery.mockResolvedValue({
    counts: { sent: 2, failed: 1, uncertain: 1 },
    recipients: [
      {
        user_id: 'bob',
        display_name: 'Bob',
        channel: 'email',
        state: 'failed',
        attempts: 1,
        error_code: 'missing_destination',
      },
    ],
  });
  api.retryGroupBriefingDelivery.mockResolvedValue({ retried: 2 });
  api.groupBriefingDownloadUrl.mockReturnValue('/api/groups/team/briefings/news/runs/3/download');
});
afterEach(cleanup);
const open = async () => {
  render(GroupBriefings, { groupId: 'team', label: 'Team', canCreate: true });
  await screen.findByText('Retained group content');
};
it('reuses the edition renderer and keeps audience, generation and delivery separate', async () => {
  await open();
  expect(screen.getByText(/Generated once per run/)).toHaveTextContent('members of Team via email');
  expect(screen.getByText(/Generation: published/)).toBeInTheDocument();
  expect(screen.getByText(/Delivery: 2 sent, 1 failed, 1 uncertain/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Download Markdown' })).toHaveAttribute(
    'href',
    '/api/groups/team/briefings/news/runs/3/download',
  );
  expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
  expect(screen.getByText(/Next run:/)).toHaveTextContent('Last run: published');
});
it('requires unchecked duplication acknowledgement and retries only the saved run', async () => {
  await open();
  const checkbox = screen.getByRole('checkbox', { name: /uncertain deliveries may be duplicated/ });
  expect(checkbox).not.toBeChecked();
  const retry = screen.getByRole('button', { name: 'Retry failed deliveries' });
  expect(retry).toBeDisabled();
  await fireEvent.click(checkbox);
  await fireEvent.click(retry);
  await screen.findByText(/2 deliveries queued from the saved edition/);
  expect(api.retryGroupBriefingDelivery).toHaveBeenCalledWith('team', 'news', 3, true);
  expect(api.runGroupBriefing).not.toHaveBeenCalled();
  expect(
    screen.getByRole('checkbox', { name: /uncertain deliveries may be duplicated/ }),
  ).not.toBeChecked();
});
it('current owner succession keeps the definition and history and removes former owner controls', async () => {
  await open();
  definition = { ...definition, capabilities: { ...caps, author_briefings: false } };
  await fireEvent.click(screen.getByRole('button', { name: 'Refresh briefings' }));
  await screen.findByText('Retained group content');
  expect(screen.queryByRole('button', { name: 'Generate now' })).toBeNull();
  expect(screen.queryByRole('link', { name: 'Edit group briefing' })).toBeNull();
  expect(screen.queryByRole('link', { name: 'New group briefing' })).toBeNull();
  definition = { ...definition, capabilities: { ...caps } };
  await fireEvent.click(screen.getByRole('button', { name: 'Refresh briefings' }));
  await screen.findByRole('button', { name: 'Generate now' });
  expect(api.getGroupBriefingRun).toHaveBeenLastCalledWith('team', 'news', 3);
  expect(screen.getByRole('link', { name: 'Edit group briefing' })).toHaveAttribute(
    'href',
    '/briefings/settings?group=team&briefing=news',
  );
});
it.each([401, 403, 404])(
  'clears displayed content and download after access loss (%s)',
  async (status) => {
    await open();
    api.getGroupBriefing.mockRejectedValue(
      Object.assign(new Error('Access unavailable'), { status }),
    );
    await fireEvent.focus(window);
    await screen.findByRole('alert');
    expect(screen.queryByText('Retained group content')).toBeNull();
    expect(screen.queryByRole('link', { name: 'Download Markdown' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry failed deliveries' })).toBeNull();
  },
);
it('refuses stale author action then lets the remaining member read and mute', async () => {
  await open();
  api.runGroupBriefing.mockRejectedValue(
    Object.assign(new Error('Current owner required'), { status: 403 }),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Generate now' }));
  await screen.findByText('Current owner required');
  expect(screen.queryByText('Retained group content')).toBeNull();
  definition = { ...definition, capabilities: { ...caps, author_briefings: false } };
  await fireEvent.click(screen.getByRole('button', { name: 'Refresh briefings' }));
  await screen.findByText('Retained group content');
  await fireEvent.click(screen.getByRole('checkbox', { name: 'Mute my external delivery' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save my delivery preferences' }));
  await waitFor(() =>
    expect(api.saveGroupBriefingPreferences).toHaveBeenCalledWith('team', 'news', {
      outbound_muted: true,
      in_app_muted: false,
    }),
  );
});
it('preview is private and has no delivery retry or publication shortcut', async () => {
  run = { ...run, mode: 'preview', state: 'preview_ready' };
  await open();
  expect(screen.getByText(/Private preview. Nothing has been sent/)).toBeInTheDocument();
  expect(api.getGroupBriefingDelivery).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Retry failed deliveries' })).toBeNull();
  api.runGroupBriefing.mockResolvedValue({ ...run, id: 9, mode: 'publish', state: 'pending' });
  await fireEvent.click(screen.getByRole('button', { name: 'Generate now' }));
  await waitFor(() =>
    expect(api.runGroupBriefing).toHaveBeenCalledWith(
      'team',
      'news',
      'publish',
      expect.any(String),
    ),
  );
});

it('ignores an old body after a membership check refuses a refresh', async () => {
  let finish!: (run: any) => void;
  api.getGroupBriefingRun.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  render(GroupBriefings, { groupId: 'team', label: 'Team' });
  await waitFor(() => expect(api.getGroupBriefingRun).toHaveBeenCalled());
  api.getGroupBriefings.mockRejectedValue(
    Object.assign(new Error('Membership ended'), { status: 404 }),
  );
  await fireEvent.focus(window);
  await screen.findByText('Membership ended');
  finish(run);
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(screen.queryByText('Retained group content')).toBeNull();
});

it('removes empty-group creation permission when the current role changes', async () => {
  api.getGroupBriefings.mockResolvedValue({ items: [], capabilities: caps });
  render(GroupBriefings, { groupId: 'team', label: 'Team', canCreate: true });
  await screen.findByRole('link', { name: 'New group briefing' });
  api.getGroupBriefings.mockResolvedValue({
    items: [],
    capabilities: { ...caps, author_briefings: false },
  });
  await fireEvent.focus(window);
  await waitFor(() =>
    expect(screen.queryByRole('link', { name: 'New group briefing' })).toBeNull(),
  );
});

it('clears author actions on a real expired-session error', async () => {
  await open();
  api.retryGroupBriefingDelivery.mockRejectedValue(new api.AuthError());
  await fireEvent.click(
    screen.getByRole('checkbox', { name: /uncertain deliveries may be duplicated/ }),
  );
  await fireEvent.click(screen.getByRole('button', { name: 'Retry failed deliveries' }));
  await screen.findByRole('alert');
  expect(screen.queryByText('Retained group content')).toBeNull();
  expect(screen.queryByRole('link', { name: 'Download Markdown' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Generate now' })).toBeNull();
});

it('clears a private preview when its author becomes a member', async () => {
  run = { ...run, mode: 'preview', state: 'preview_ready' };
  await open();
  definition = { ...definition, capabilities: { ...caps, author_briefings: false } };
  api.getGroupBriefingRun.mockRejectedValue(
    Object.assign(new Error('Edition not found'), { status: 404 }),
  );
  await fireEvent.focus(window);
  await screen.findByText('Edition not found');
  expect(screen.queryByText('Retained group content')).toBeNull();
  expect(screen.queryByRole('link', { name: 'Download Markdown' })).toBeNull();
});

it('lets a member change group-wide delivery defaults without leaving the group', async () => {
  api.getGroupDeliveryPreferences.mockResolvedValue({ outbound_muted: true, in_app_muted: true });
  api.saveGroupDeliveryPreferences.mockImplementation(async (_group, prefs) => prefs);
  await open();
  const outbound = screen.getByRole('checkbox', {
    name: 'Mute external delivery for all briefings in this group',
  });
  expect(outbound).toBeChecked();
  await fireEvent.click(outbound);
  await fireEvent.click(screen.getByRole('button', { name: 'Save group delivery preferences' }));
  await waitFor(() =>
    expect(api.saveGroupDeliveryPreferences).toHaveBeenCalledWith('team', {
      outbound_muted: false,
      in_app_muted: true,
    }),
  );
  expect(screen.getByText('Retained group content')).toBeInTheDocument();
});

it('shows excluded delivery legs separately from mutes and missing destinations', async () => {
  api.getGroupBriefingDelivery.mockResolvedValue({
    counts: { sent: 2, skipped: 4 },
    reasons: { excluded: 2, muted: 1, missing_destination: 1 },
    recipients: [],
  });
  await open();
  expect(screen.getByText('2 excluded · 1 muted · 1 missing destinations')).toBeVisible();
});
