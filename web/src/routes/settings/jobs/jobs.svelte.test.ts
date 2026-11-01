import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { __history } from '../../../../vitest-stubs/app-navigation';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
const notices = vi.hoisted(() => ({
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('@minot/ui/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...notices,
}));
await fillApiDouble(api, {});
import { AuthError } from '$lib/api';
import { USER_SETTINGS_SECTIONS } from '$lib/settings/sections';
import Harness from './jobsHarness.test.svelte';

const LIST = '/istota/settings/jobs';
const CONFIG_EDITOR = '/istota/files?view=config';
const currentUrl = () => __history.entries[__history.index].url;

function network(name: string, overrides = {}) {
  return {
    subject: `cron:${name}`,
    kind: 'cron',
    label: `Scheduled job: ${name}`,
    state: 'limited',
    by_default: null,
    hosts: [],
    skills: [],
    approvals: [],
    job: {
      run_state: 'running',
      digest: 'a'.repeat(64),
      once: false,
      waiting: false,
      reach: 'private',
      settable: true,
      needs_approval: { public: true, open: true },
      prompt: 'Read the news and summarise it.',
      command: '',
      skill: '',
      skill_args: '',
      publish_shared_kv: '',
      publish_shared_kv_trusted: false,
    },
    waiting_on: [],
    refused: [],
    ...overrides,
  };
}

function job(overrides: Record<string, unknown> = {}) {
  const name = (overrides.name as string) ?? 'digest';
  return {
    id: 1,
    pending: false,
    name,
    cron: '0 9 * * *',
    enabled: true,
    once: false,
    state: 'running',
    last_run_at: null,
    last_success_at: null,
    consecutive_failures: 0,
    last_error: null,
    definition: {
      kind: 'prompt',
      prompt: 'Read the news and summarise it.',
      prompt_file: '',
      command: '',
      target: 'web',
      room: '',
      silent_unless_action: false,
      skip_log_channel: false,
      model: '',
      effort: '',
      brain: '',
      provider: '',
      publish_shared_kv: '',
      publish_shared_kv_trusted: false,
      egress: [],
      egress_kept: [],
    },
    revision: 'r1',
    editable: true,
    network: network(name),
    ...overrides,
  };
}

function listing(jobs: object[], file = { readable: true, errors: [] as string[] }) {
  return {
    jobs,
    file,
    options: {
      outputs: [],
      rooms: { talk: [], web: [] },
      selectable_brains: [],
      can_publish: false,
    },
    external_skills: ['browse'],
    timezone: 'UTC',
  };
}

const expireSession = vi.fn();

function open(url: string, ...jobs: object[]) {
  __history.reset(url);
  page.url = new URL(url, 'http://localhost');
  page.state = {};
  api.getScheduledJobs.mockResolvedValue(listing(jobs));
  render(Harness, { expireSession });
}

async function row(name = 'digest') {
  const cell = await screen.findByRole('link', { name });
  return within(cell.closest('tr') as HTMLElement);
}

async function kebabItem(name: string, label: string) {
  const view = await row(name);
  await fireEvent.click(view.getByRole('button', { name: `Actions for ${name}` }));
  return screen.findByRole('menuitem', { name: label });
}

beforeEach(() => {
  api.setScheduledJobEnabled.mockResolvedValue({ outcome: 'file' });
  api.runScheduledJob.mockResolvedValue({ task_id: 9 });
  api.deleteScheduledJob.mockResolvedValue({ deleted: true, id: 1 });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('takes the place of Network access in the sections', () => {
  const hrefs = USER_SETTINGS_SECTIONS.map((section) => section.href);
  expect(hrefs).toContain('/jobs');
  expect(hrefs).not.toContain('/network');
  expect(USER_SETTINGS_SECTIONS.find((s) => s.href === '/jobs')?.label).toBe('Scheduled jobs');
});

it('lists a job with its schedule, state, access, failures and last run', async () => {
  open(
    LIST,
    job({ last_run_at: '2026-10-01T09:00:00Z', consecutive_failures: 2 }),
    job({ id: 2, name: 'weekly', state: 'disabled', enabled: false }),
    job({ id: 3, name: 'flaky', state: 'suspended' }),
    job({ id: 4, name: 'open', network: network('open', { state: 'open' }) }),
  );
  const view = await row();
  expect(view.getByText('0 9 * * *')).toBeInTheDocument();
  expect(view.getByText('Running')).toBeInTheDocument();
  expect(view.getByText('Limited')).toBeInTheDocument();
  expect(view.getByText('2')).toBeInTheDocument();
  expect(view.getByRole('link', { name: 'digest' })).toHaveAttribute('href', `${LIST}?job=1`);
  expect((await row('weekly')).getByText('Disabled')).toBeInTheDocument();
  expect((await row('flaky')).getByText('Suspended')).toBeInTheDocument();
  expect((await row('open')).getByText('Full access')).toBeInTheDocument();
});

it('says Never for a job that has not run, and marks a one-time job', async () => {
  open(LIST, job({ once: true }));
  const view = await row();
  expect(view.getByText('Never')).toBeInTheDocument();
  expect(view.getByText('One time')).toBeInTheDocument();
});

it('has an empty state and no alert with nothing to list', async () => {
  open(LIST);
  expect(await screen.findByText(/no scheduled jobs/i)).toBeInTheDocument();
  expect(screen.queryByRole('table')).toBeNull();
  expect(screen.queryByRole('note')).toBeNull();
});

it('a pending row waits for the next sync with no link or actions', async () => {
  open(LIST, job({ id: null, pending: true, network: null, revision: 'r1' }));
  const name = await screen.findByText('digest');
  const view = within(name.closest('tr') as HTMLElement);
  expect(view.getByText('Waiting for the next sync')).toBeInTheDocument();
  expect(view.queryByRole('link')).toBeNull();
  expect(view.queryByRole('button')).toBeNull();
});

it('a command job the sync drops keeps its error and points at CRON.md', async () => {
  open(
    LIST,
    job({
      id: null,
      pending: false,
      editable: false,
      revision: null,
      network: null,
      file_error: 'Command jobs are admin-only, so this job does not run.',
    }),
  );
  const name = await screen.findByText('digest');
  const view = within(name.closest('tr') as HTMLElement);
  expect(view.getByText(/Command jobs are admin-only/)).toBeInTheDocument();
  expect(view.queryByText('Running')).toBeNull();
  expect(view.getByText('Not running')).toBeInTheDocument();
  expect(view.getByRole('link', { name: /CRON\.md/ })).toHaveAttribute('href', CONFIG_EDITOR);
  expect(view.queryByText('Waiting for the next sync')).toBeNull();
  expect(view.queryByRole('button')).toBeNull();
});

it('warns that jobs are read only while CRON.md has a problem, naming each', async () => {
  __history.reset(LIST);
  page.url = new URL(LIST, 'http://localhost');
  page.state = {};
  api.getScheduledJobs.mockResolvedValue(
    listing([job({ editable: false, revision: null })], {
      readable: false,
      errors: ["Job 'broken' has no cron."],
    }),
  );
  render(Harness, { expireSession });
  expect(await screen.findByText(/read only/i)).toBeInTheDocument();
  expect(screen.getByText("Job 'broken' has no cron.")).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /Configuration/ })).toHaveAttribute(
    'href',
    CONFIG_EDITOR,
  );
});

it('a read-only job can be disabled but not deleted', async () => {
  open(LIST, job({ editable: false, revision: null }));
  expect(await kebabItem('digest', 'Disable')).toBeInTheDocument();
  expect(screen.queryByRole('menuitem', { name: 'Delete' })).toBeNull();
});

it("a module's job can only be opened", async () => {
  open(LIST, job({ name: '_module.feeds.poll', editable: false, revision: null }));
  expect(await kebabItem('_module.feeds.poll', 'View')).toBeInTheDocument();
  expect(screen.queryByRole('menuitem', { name: 'Run now' })).toBeNull();
  expect(screen.queryByRole('menuitem', { name: 'Disable' })).toBeNull();
  expect(screen.queryByRole('menuitem', { name: 'Delete' })).toBeNull();
});

it('Disable goes through the route and reloads', async () => {
  open(LIST, job());
  await fireEvent.click(await kebabItem('digest', 'Disable'));
  await waitFor(() => expect(api.setScheduledJobEnabled).toHaveBeenCalledWith(1, false));
  await waitFor(() => expect(api.getScheduledJobs).toHaveBeenCalledTimes(2));
});

it.each(['disabled', 'suspended'])('Enable is what a %s job offers', async (state) => {
  open(LIST, job({ state }));
  await fireEvent.click(await kebabItem('digest', 'Enable'));
  await waitFor(() => expect(api.setScheduledJobEnabled).toHaveBeenCalledWith(1, true));
});

it('warns when the file could not take the change', async () => {
  api.setScheduledJobEnabled.mockResolvedValue({ outcome: 'db_only' });
  open(LIST, job());
  await fireEvent.click(await kebabItem('digest', 'Disable'));
  await waitFor(() =>
    expect(notices.notifyWarning).toHaveBeenCalledWith(expect.stringMatching(/next sync/)),
  );
});

it('Run now starts the job, and a refusal is a notice with its reason', async () => {
  open(LIST, job());
  await fireEvent.click(await kebabItem('digest', 'Run now'));
  await waitFor(() => expect(api.runScheduledJob).toHaveBeenCalledWith(1));
  await waitFor(() => expect(notices.notifySuccess).toHaveBeenCalled());

  api.runScheduledJob.mockRejectedValueOnce(
    Object.assign(new Error('That job ran a minute ago.'), { status: 409 }),
  );
  await fireEvent.click(await kebabItem('digest', 'Run now'));
  await waitFor(() =>
    expect(notices.notifyError).toHaveBeenCalledWith('That job ran a minute ago.'),
  );
});

it('Delete asks first, and a refusal stays in the dialog', async () => {
  api.deleteScheduledJob.mockRejectedValueOnce(
    Object.assign(new Error('The job is still in CRON.md.'), { status: 409 }),
  );
  open(LIST, job());
  await fireEvent.click(await kebabItem('digest', 'Delete'));
  const dialog = within(await screen.findByRole('dialog'));
  expect(api.deleteScheduledJob).not.toHaveBeenCalled();
  await fireEvent.click(dialog.getByRole('button', { name: 'Delete' }));
  expect(await dialog.findByText('The job is still in CRON.md.')).toBeInTheDocument();

  api.getScheduledJobs.mockResolvedValue(listing([]));
  await fireEvent.click(dialog.getByRole('button', { name: 'Delete' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(api.deleteScheduledJob).toHaveBeenCalledWith(1);
  expect(await screen.findByText(/no scheduled jobs/i)).toBeInTheDocument();
});

it('a deep link opens the job, which is where the bell lands', async () => {
  open(
    `${LIST}?job=1`,
    job({
      last_error: 'timed out',
      network: network('digest', {
        refused: [{ entry: 'news.example.com', hits: 1, last_seen: '2026-10-30' }],
      }),
    }),
  );
  expect(await screen.findByRole('heading', { name: 'Network access' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'digest' })).toBeInTheDocument();
  expect(screen.getByText('news.example.com')).toBeInTheDocument();
  expect(screen.getByText('0 9 * * *')).toBeInTheDocument();
  expect(screen.getByText('timed out')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Scheduled jobs' })).toHaveAttribute('href', LIST);
  expect(screen.queryByRole('table')).toBeNull();
  // The definition is on the page, so the card does not repeat it.
  expect(screen.queryByText('Job text')).toBeNull();
  expect(screen.getByText('Read the news and summarise it.')).toBeInTheDocument();
});

it('the detail view shows the definition read only', async () => {
  open(`${LIST}?job=1`, job());
  await screen.findByRole('heading', { name: 'Network access' });
  expect(screen.queryByRole('textbox', { name: 'Name' })).toBeNull();
  expect(screen.queryByRole('button', { name: /save/i })).toBeNull();
  expect(screen.getByRole('link', { name: /CRON\.md/ })).toHaveAttribute('href', CONFIG_EDITOR);
});

it('a last error and a prompt are text, never markup', async () => {
  open(
    `${LIST}?job=1`,
    job({
      last_error: '<img src=x onerror=alert(1)> [a](https://evil.example)',
      definition: { ...job().definition, prompt: '<script>alert(1)</script>' },
    }),
  );
  expect(await screen.findByText(/<img src=x/)).toBeInTheDocument();
  expect(screen.getByText('<script>alert(1)</script>')).toBeInTheDocument();
  expect(document.querySelector('img[src="x"]')).toBeNull();
  expect(screen.queryByRole('link', { name: 'a' })).toBeNull();
});

it('explains a disabled job and offers Enable on its page', async () => {
  open(`${LIST}?job=1`, job({ state: 'disabled', enabled: false }));
  expect(await screen.findByText(/does not run/)).toBeInTheDocument();
  await fireEvent.click(screen.getByRole('button', { name: 'Enable' }));
  await waitFor(() => expect(api.setScheduledJobEnabled).toHaveBeenCalledWith(1, true));
});

it('explains a suspended job and does not send anyone to chat', async () => {
  open(`${LIST}?job=1`, job({ state: 'suspended' }));
  expect(await screen.findByText(/repeated failures/)).toBeInTheDocument();
  expect(screen.queryByText('!cron enable')).toBeNull();
  expect(screen.getByRole('button', { name: 'Enable' })).toBeInTheDocument();
});

it('View opens the job and a same-route link goes back to the list', async () => {
  open(LIST, job());
  await fireEvent.click(await kebabItem('digest', 'View'));
  expect(await screen.findByRole('heading', { name: 'Network access' })).toBeInTheDocument();
  expect(currentUrl()).toBe(`${LIST}?job=1`);

  __history.reset(LIST);
  page.url = new URL(LIST, 'http://localhost');
  page.state = {};
  expect(await screen.findByRole('table')).toBeInTheDocument();
});

it('Back returns to the list', async () => {
  open(LIST, job());
  await fireEvent.click(await kebabItem('digest', 'View'));
  await screen.findByRole('heading', { name: 'Network access' });
  __history.back();
  expect(await screen.findByRole('table')).toBeInTheDocument();
});

it('a job that has gone falls back to the list with a notice', async () => {
  open(`${LIST}?job=99`, job());
  expect(await screen.findByRole('table')).toBeInTheDocument();
  expect(currentUrl()).toBe(LIST);
  expect(notices.notifyWarning).toHaveBeenCalledWith(expect.stringMatching(/no longer exists/));
});

it('a one-time job that fired while its page was open refreshes back to the list', async () => {
  api.runScheduledJob.mockRejectedValueOnce(
    Object.assign(new Error('There is no such item.'), { status: 404 }),
  );
  open(`${LIST}?job=1`, job({ once: true }));
  const run = await screen.findByRole('button', { name: 'Run now' });
  api.getScheduledJobs.mockResolvedValue(listing([job({ id: 2, name: 'other' })]));
  await fireEvent.click(run);
  expect(await screen.findByRole('table')).toBeInTheDocument();
  expect(currentUrl()).toBe(LIST);
  expect(notices.notifyWarning).toHaveBeenCalledTimes(1);
  expect(notices.notifyError).not.toHaveBeenCalled();
});

it('deleting from the page returns to the list', async () => {
  open(`${LIST}?job=1`, job());
  await fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
  const dialog = within(await screen.findByRole('dialog'));
  api.getScheduledJobs.mockResolvedValue(listing([]));
  await fireEvent.click(dialog.getByRole('button', { name: 'Delete' }));
  expect(await screen.findByText(/no scheduled jobs/i)).toBeInTheDocument();
  expect(currentUrl()).toBe(LIST);
  expect(notices.notifyWarning).not.toHaveBeenCalled();
});

it('a change on the card reloads the listing', async () => {
  api.addNetworkGrant.mockResolvedValue({ ok: true });
  open(
    `${LIST}?job=1`,
    job({
      network: network('digest', {
        refused: [{ entry: 'news.example.com', hits: 1, last_seen: '2026-10-30' }],
      }),
    }),
  );
  await fireEvent.click(await screen.findByRole('button', { name: 'Add news.example.com' }));
  await waitFor(() => expect(api.getScheduledJobs).toHaveBeenCalledTimes(2));
});

it('an expired session goes to the session handler', async () => {
  __history.reset(LIST);
  api.getScheduledJobs.mockRejectedValue(new AuthError());
  render(Harness, { expireSession });
  await waitFor(() => expect(expireSession).toHaveBeenCalled());
});

it('a failed load offers a retry', async () => {
  __history.reset(LIST);
  page.url = new URL(LIST, 'http://localhost');
  page.state = {};
  api.getScheduledJobs.mockRejectedValueOnce(new Error('Scheduled jobs could not be loaded.'));
  api.getScheduledJobs.mockResolvedValue(listing([job()]));
  render(Harness, { expireSession });
  expect(await screen.findByText('Scheduled jobs could not be loaded.')).toBeInTheDocument();
  await fireEvent.click(screen.getByRole('button', { name: /retry/i }));
  expect(await screen.findByRole('table')).toBeInTheDocument();
});

it('a failed reload keeps the list and says so', async () => {
  open(LIST, job());
  await row();
  api.setScheduledJobEnabled.mockResolvedValue({ outcome: 'file' });
  api.getScheduledJobs.mockRejectedValueOnce(new Error('Scheduled jobs could not be loaded.'));
  await fireEvent.click(await kebabItem('digest', 'Disable'));
  await waitFor(() =>
    expect(notices.notifyError).toHaveBeenCalledWith('Scheduled jobs could not be loaded.'),
  );
  expect(screen.getByRole('table')).toBeInTheDocument();
});
