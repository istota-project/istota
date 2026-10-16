import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
// The real store arms auto-dismiss timers that outlive the test.
const notices = vi.hoisted(() => ({
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('$lib/stores/notices', () => notices);
import Page from './+page.svelte';

const job = (over: Record<string, unknown>) => ({
  id: 1,
  user_id: 'bob',
  name: 'digest',
  cron: '0 9 * * *',
  enabled: true,
  auto_disabled_at: null,
  last_run_at: null,
  last_success_at: null,
  consecutive_failures: 0,
  last_error: null,
  ...over,
});
const jobs = () => [
  job({}),
  job({ id: 2, name: 'weekly', enabled: false }),
  job({
    id: 3,
    user_id: 'carol',
    name: 'flaky',
    auto_disabled_at: '2026-10-10T09:30:00Z',
    consecutive_failures: 5,
    last_error: 'timed out',
  }),
  job({ id: 4, user_id: 'carol', name: '_module.feeds.run_scheduled', cron: '*/5 * * * *' }),
];

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.getAdminScheduledJobs.mockImplementation(async () => ({
    jobs_total: 4,
    jobs_active: 2,
    jobs_paused: 2,
    jobs: jobs(),
  }));
  api.setAdminScheduledJobEnabled.mockResolvedValue({ outcome: 'file', job: null });
  api.deleteAdminScheduledJob.mockResolvedValue({ deleted: true, id: 1 });
  api.startStepUp.mockResolvedValue({
    request_id: 'req-1',
    method: 'email',
    email_hint: 'a***@example.com',
  });
});

const userTable = async () => screen.findByRole('table', { name: 'User jobs' });
const moduleTable = async () => screen.findByRole('table', { name: 'Module pollers' });

it('splits user jobs from module pollers and labels the three states', async () => {
  render(Page);
  const users = await userTable();
  const digest = within(users).getByRole('row', { name: /digest/ });
  expect(digest).toHaveTextContent('bob');
  expect(digest).toHaveTextContent('0 9 * * *');
  expect(within(digest).getByText('Running').classList.contains('badge')).toBe(true);
  expect(within(users).getByRole('row', { name: /weekly/ })).toHaveTextContent('Disabled');
  const flaky = within(users).getByRole('row', { name: /flaky/ });
  expect(flaky).toHaveTextContent('Suspended');
  expect(flaky).toHaveTextContent('timed out');
  expect(within(users).queryByRole('row', { name: /_module/ })).toBeNull();
  expect(within(await moduleTable()).getByRole('row', { name: /_module\.feeds/ })).toBeTruthy();
});

it('filters by user or job name', async () => {
  render(Page);
  await userTable();
  await fireEvent.input(screen.getByRole('searchbox', { name: 'Filter scheduled jobs' }), {
    target: { value: 'carol' },
  });
  const users = await userTable();
  expect(within(users).queryByRole('row', { name: /digest/ })).toBeNull();
  expect(within(users).getByRole('row', { name: /flaky/ })).toBeTruthy();
});

it('disables only after a confirmation, then reloads', async () => {
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for bob/digest' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Disable' }));
  const dialog = await screen.findByRole('dialog', { name: 'Disable job' });
  expect(dialog).toHaveTextContent('Disable digest for bob?');
  expect(api.setAdminScheduledJobEnabled).not.toHaveBeenCalled();
  await fireEvent.click(within(dialog).getByRole('button', { name: 'Disable' }));
  await waitFor(() => expect(api.setAdminScheduledJobEnabled).toHaveBeenCalledWith(1, false));
  await waitFor(() => expect(api.getAdminScheduledJobs).toHaveBeenCalledTimes(2));
  expect(notices.notifyWarning).not.toHaveBeenCalled();
});

it('enables a suspended job without a confirmation', async () => {
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for carol/flaky' }));
  const items = await screen.findAllByRole('menuitem');
  expect(items.map((item) => item.textContent?.trim())).toEqual(['Enable', 'Delete']);
  await fireEvent.click(items[0]);
  await waitFor(() => expect(api.setAdminScheduledJobEnabled).toHaveBeenCalledWith(3, true));
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('warns when CRON.md could not be rewritten', async () => {
  api.setAdminScheduledJobEnabled.mockResolvedValueOnce({ outcome: 'db_only', job: null });
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for bob/weekly' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Enable' }));
  await waitFor(() => expect(notices.notifyWarning).toHaveBeenCalledTimes(1));
  expect(notices.notifyWarning.mock.calls[0][0]).toMatch(/CRON\.md could not be updated/);
});

it('offers no delete on a module poller', async () => {
  render(Page);
  await fireEvent.click(
    await screen.findByRole('button', { name: 'Actions for carol/_module.feeds.run_scheduled' }),
  );
  const items = await screen.findAllByRole('menuitem');
  expect(items.map((item) => item.textContent?.trim())).toEqual(['Disable']);
});

it('deletes through the typed name and a step-up proof', async () => {
  render(Page);
  await fireEvent.click(await screen.findByRole('button', { name: 'Actions for bob/digest' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
  const dialog = await screen.findByRole('dialog', { name: 'Delete job' });
  const confirm = within(dialog).getByRole('button', { name: 'Delete' });
  expect(confirm).toBeDisabled();
  await fireEvent.input(within(dialog).getByLabelText('Type to confirm'), {
    target: { value: 'digest' },
  });
  await fireEvent.click(confirm);
  const stepUp = await screen.findByRole('dialog', { name: 'Confirm sensitive action' });
  await waitFor(() => expect(api.startStepUp).toHaveBeenCalledWith('admin_cron_delete', undefined));
  await fireEvent.input(await within(stepUp).findByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(within(stepUp).getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(api.deleteAdminScheduledJob).toHaveBeenCalledWith(1, 'digest', {
      request_id: 'req-1',
      code: '123456',
    }),
  );
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  await waitFor(() => expect(api.getAdminScheduledJobs).toHaveBeenCalledTimes(2));
  expect(notices.notifyError).not.toHaveBeenCalled();
});
