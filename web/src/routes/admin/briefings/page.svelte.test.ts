import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
// The real store arms auto-dismiss timers that outlive the test.
const notices = vi.hoisted(() => ({ notifyError: vi.fn(), notifySuccess: vi.fn() }));
vi.mock('@minot/ui/stores/notices', () => notices);
import Page from './+page.svelte';

const held = { at: '2026-10-10T09:30:00+00:00', by_label: 'Alice' };
const personal = () => [
  {
    id: 201,
    user_id: 'carol',
    owner_label: 'Carol',
    name: 'weekly',
    title: 'Weekly Review',
    cron: '0 9 * * 1',
    timezone: 'Europe/Berlin',
    output: 'email',
    enabled: true,
    held: null,
    last_run_at: null,
  },
  {
    id: 202,
    user_id: 'dave',
    owner_label: 'Dave',
    name: 'weekend',
    title: 'Weekend Reading',
    cron: '0 10 * * 6',
    timezone: 'UTC',
    output: 'talk',
    enabled: true,
    held,
    last_run_at: null,
  },
  {
    id: 203,
    user_id: 'erin',
    owner_label: 'Erin',
    name: 'evening',
    title: '',
    cron: '0 18 * * *',
    timezone: null,
    output: 'ntfy',
    enabled: false,
    held: null,
    last_run_at: null,
  },
];
const group = () => [
  {
    id: 301,
    group_id: 'team',
    group_label: 'Team',
    system_key: null,
    name: 'daily',
    title: 'Team daily',
    cron: '0 8 * * 1-5',
    timezone: 'Europe/Berlin',
    channel: 'in_app',
    enabled: true,
    held: null,
    owners: ['Carol'],
    last_published_at: null,
  },
];

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.getAdminBriefings.mockImplementation(async () => ({ personal: personal(), group: group() }));
  api.holdAdminBriefing.mockResolvedValue({});
  api.resumeAdminBriefing.mockResolvedValue({});
  api.deleteAdminBriefing.mockResolvedValue({ ok: true, deleted: true, id: 301 });
  api.startStepUp.mockResolvedValue({
    request_id: 'req-1',
    method: 'email',
    email_hint: 'a***@example.com',
  });
});

const personalTable = async () => screen.findByRole('table', { name: 'Personal briefings' });
const groupTable = async () => screen.findByRole('table', { name: 'Group briefings' });

it('lists both kinds with schedule, delivery and one state badge each', async () => {
  render(Page);
  const people = await personalTable();
  const weekly = within(people).getByRole('row', { name: /Weekly Review/ });
  expect(weekly).toHaveTextContent('Carol');
  expect(weekly).toHaveTextContent('0 9 * * 1');
  expect(weekly).toHaveTextContent('Europe/Berlin');
  expect(weekly).toHaveTextContent('Email');
  expect(within(weekly).getByText('Running').classList.contains('badge')).toBe(true);
  const weekend = within(people).getByRole('row', { name: /Weekend Reading/ });
  expect(within(weekend).getByText('Paused by admin').classList.contains('badge')).toBe(true);
  // A held briefing reads as paused even though the owner left it enabled;
  // a blank title falls back to the name's derived one.
  const evening = within(people).getByRole('row', { name: /Evening Briefing/ });
  expect(within(evening).getByText('Off')).toBeTruthy();

  const team = within(await groupTable()).getByRole('row', { name: /Team daily/ });
  expect(team).toHaveTextContent('Team');
  expect(team).toHaveTextContent('In app');
  expect(within(team).getByText('Running')).toBeTruthy();
});

it('lays out as a full-width card pane like the other admin panes', async () => {
  render(Page);
  const table = await personalTable();
  expect(table.closest('.admin-page')?.classList.contains('card-pane')).toBe(true);
});

it('filters both lists by owner, group and title', async () => {
  render(Page);
  await personalTable();
  await fireEvent.input(screen.getByRole('searchbox', { name: 'Filter briefings' }), {
    target: { value: 'dave' },
  });
  const people = await personalTable();
  expect(within(people).queryByRole('row', { name: /Weekly Review/ })).toBeNull();
  expect(within(people).getByRole('row', { name: /Weekend Reading/ })).toBeTruthy();
  expect(screen.queryByRole('table', { name: 'Group briefings' })).toBeNull();
  expect(screen.getByText('No group briefings match the filter.')).toBeTruthy();

  await fireEvent.input(screen.getByRole('searchbox', { name: 'Filter briefings' }), {
    target: { value: 'team' },
  });
  expect(within(await groupTable()).getByRole('row', { name: /Team daily/ })).toBeTruthy();
});

it('pauses only after a confirmation naming the owner, then reloads', async () => {
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for Weekly Review' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Pause' }));
  const dialog = await screen.findByRole('dialog', { name: 'Pause briefing' });
  expect(dialog).toHaveTextContent('Pause Weekly Review for Carol?');
  expect(dialog).toHaveTextContent('until an administrator resumes it');
  expect(api.holdAdminBriefing).not.toHaveBeenCalled();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Pause' }));
  await waitFor(() => expect(api.holdAdminBriefing).toHaveBeenCalledWith('personal', 201));
  await waitFor(() => expect(api.getAdminBriefings).toHaveBeenCalledTimes(2));
});

it('resumes a held briefing without a confirmation', async () => {
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for Weekend Reading' }));
  const items = await screen.findAllByRole('menuitem');
  expect(items.map((item) => item.textContent?.trim())).toEqual(['Resume', 'Delete']);
  await fireEvent.click(items[0]);
  await waitFor(() => expect(api.resumeAdminBriefing).toHaveBeenCalledWith('personal', 202));
  expect(screen.queryByRole('dialog')).toBeNull();
  await waitFor(() => expect(api.getAdminBriefings).toHaveBeenCalledTimes(2));
});

async function startDelete(rowTitle: string, name: string) {
  await fireEvent.click(await screen.findByRole('button', { name: `Actions for ${rowTitle}` }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
  const dialog = await screen.findByRole('dialog', { name: 'Delete briefing' });
  const confirm = within(dialog).getByRole('button', { name: 'Delete' });
  expect(confirm).toBeDisabled();
  await fireEvent.input(within(dialog).getByLabelText('Type to confirm'), {
    target: { value: name },
  });
  expect(confirm).toBeEnabled();
  await fireEvent.click(confirm);
  const stepUp = await screen.findByRole('dialog', { name: 'Confirm sensitive action' });
  await waitFor(() =>
    expect(api.startStepUp).toHaveBeenCalledWith('admin_briefing_delete', undefined),
  );
  await fireEvent.input(await within(stepUp).findByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(within(stepUp).getByRole('button', { name: 'Confirm' }));
  return stepUp;
}

it('deletes through the typed name and a step-up proof', async () => {
  render(Page);
  await startDelete('Team daily', 'daily');
  await waitFor(() =>
    expect(api.deleteAdminBriefing).toHaveBeenCalledWith('group', 301, 'daily', {
      request_id: 'req-1',
      code: '123456',
    }),
  );
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await waitFor(() => expect(api.getAdminBriefings).toHaveBeenCalledTimes(2));
  expect(notices.notifyError).not.toHaveBeenCalled();
});

it('keeps a refused code in the step-up dialog', async () => {
  api.deleteAdminBriefing.mockRejectedValueOnce(
    new api.GroupApiError(403, '', 'That code was not accepted. Try again.', 'bad'),
  );
  render(Page);
  const stepUp = await startDelete('Weekly Review', 'weekly');
  expect(await within(stepUp).findByRole('alert')).toHaveTextContent('not accepted');
  expect(screen.getByRole('dialog', { name: 'Confirm sensitive action' })).toBeTruthy();
  expect(notices.notifyError).not.toHaveBeenCalled();
});

it('reports a pending migration with the step to take', async () => {
  api.deleteAdminBriefing.mockRejectedValueOnce(
    new api.GroupApiError(
      409,
      'migration_pending',
      "Finish or roll back the briefing's migration first",
    ),
  );
  render(Page);
  await startDelete('Team daily', 'daily');
  await waitFor(() => expect(notices.notifyError).toHaveBeenCalledTimes(1));
  expect(notices.notifyError.mock.calls[0][0]).toMatch(/istota briefings migrate/);
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('reports a failed pause with a notice', async () => {
  api.holdAdminBriefing.mockRejectedValueOnce(
    new api.GroupApiError(404, 'briefing_not_found', 'Briefing not found'),
  );
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for Team daily' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Pause' }));
  const dialog = await screen.findByRole('dialog', { name: 'Pause briefing' });
  expect(dialog).toHaveTextContent('Pause Team daily for Team?');
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Pause' }));
  await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('Briefing not found'));
  await waitFor(() => expect(api.getAdminBriefings).toHaveBeenCalledTimes(2));
});
