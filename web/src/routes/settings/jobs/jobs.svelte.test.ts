import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
import { AuthError, JobSaveError } from '$lib/api';
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
      outputs: ['email', 'talk', 'web'],
      rooms: {
        talk: [{ token: 'tk1', name: 'Team', channel: false, shared: false }],
        web: [{ token: 'rm1', name: 'General', default: true, shared: false, channel: false }],
      },
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
  api.getJobModelChoices.mockResolvedValue({
    model_aliases: [],
    models: [],
    default_model: null,
    providers: [],
  });
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
  expect(screen.getByDisplayValue('Read the news and summarise it.')).toBeInTheDocument();
});

it('a job that cannot be written shows the form read only, with no Save', async () => {
  open(`${LIST}?job=1`, job({ editable: false, revision: null }));
  await screen.findByRole('heading', { name: 'Network access' });
  expect(screen.getByRole('textbox', { name: 'Name' })).toBeDisabled();
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
  expect(screen.getByDisplayValue('<script>alert(1)</script>')).toBeInTheDocument();
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

it('Edit opens the job and a same-route link goes back to the list', async () => {
  open(LIST, job());
  await fireEvent.click(await kebabItem('digest', 'Edit'));
  expect(await screen.findByRole('heading', { name: 'Network access' })).toBeInTheDocument();
  expect(currentUrl()).toBe(`${LIST}?job=1`);

  __history.reset(LIST);
  page.url = new URL(LIST, 'http://localhost');
  page.state = {};
  expect(await screen.findByRole('table')).toBeInTheDocument();
});

it('Back returns to the list', async () => {
  open(LIST, job());
  await fireEvent.click(await kebabItem('digest', 'Edit'));
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

describe('the form', () => {
  const textbox = (name: string) => screen.findByRole('textbox', { name });
  const refused = (
    status: number,
    code: string,
    detail: string,
    extra: { field?: string; errors?: string[] } = {},
  ) => new JobSaveError(detail, status, code, extra.field ?? null, extra.errors ?? []);

  async function edit(name: string, value: string) {
    await fireEvent.input(await textbox(name), { target: { value } });
  }

  async function fillNew() {
    await edit('Name', 'morning');
    await edit('Schedule', '0 7 * * *');
    await edit('Prompt', 'Say good morning.');
  }

  const save = async () =>
    fireEvent.click(await screen.findByRole('button', { name: /^(Save|Create job)$/ }));

  function approvedJob(state = 'open', overrides = {}) {
    return job({
      network: network('digest', { state, approvals: [`${state}:${'a'.repeat(64)}`] }),
      ...overrides,
    });
  }

  beforeEach(() => {
    api.createScheduledJob.mockResolvedValue({ applies: 'next_sync' });
    api.updateScheduledJob.mockResolvedValue({ applies: 'next_sync', access_kept: null });
  });

  it('New job opens the form under its own address', async () => {
    open(LIST, job());
    await fireEvent.click(await screen.findByRole('button', { name: 'New job' }));
    expect(await screen.findByRole('heading', { name: 'New job' })).toBeInTheDocument();
    expect(currentUrl()).toBe(`${LIST}?job=new`);
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.getByText(/UTC/)).toBeInTheDocument();
  });

  it('the empty state offers New job', async () => {
    open(LIST);
    await screen.findByText(/no scheduled jobs/i);
    const offered = screen.getAllByRole('button', { name: 'New job' });
    await fireEvent.click(offered[offered.length - 1]);
    expect(await screen.findByRole('heading', { name: 'New job' })).toBeInTheDocument();
  });

  it('a create sends the whole job and returns to the list', async () => {
    open(`${LIST}?job=new`);
    await screen.findByRole('heading', { name: 'New job' });
    await fillNew();
    await save();
    await waitFor(() => expect(api.createScheduledJob).toHaveBeenCalled());
    expect(api.createScheduledJob.mock.calls[0][0]).toEqual({
      name: 'morning',
      cron: '0 7 * * *',
      kind: 'prompt',
      prompt: 'Say good morning.',
      target: 'web',
      room: '',
      enabled: true,
      once: false,
      silent_unless_action: false,
      skip_log_channel: false,
      reach: 'limited',
    });
    await waitFor(() => expect(currentUrl()).toBe(LIST));
    await waitFor(() => expect(api.getScheduledJobs).toHaveBeenCalledTimes(2));
  });

  it('access is offered on create only, and says what Full asks for', async () => {
    open(`${LIST}?job=new`);
    await screen.findByRole('heading', { name: 'New job' });
    await fireEvent.click(screen.getByRole('radio', { name: 'Full access' }));
    expect(screen.getByText(/asks for approval on the job's page/)).toBeInTheDocument();
    await fillNew();
    await save();
    await waitFor(() =>
      expect(api.createScheduledJob).toHaveBeenCalledWith(
        expect.objectContaining({ reach: 'full' }),
      ),
    );
  });

  it('a command job sends its command and not a prompt', async () => {
    open(`${LIST}?job=new`);
    await screen.findByRole('heading', { name: 'New job' });
    await edit('Name', 'tick');
    await edit('Schedule', '* * * * *');
    await fireEvent.click(screen.getByRole('radio', { name: 'Command' }));
    await edit('Command', 'istota-skill kv list');
    expect(screen.queryByRole('textbox', { name: 'Prompt' })).toBeNull();
    await save();
    await waitFor(() => expect(api.createScheduledJob).toHaveBeenCalled());
    const sent = api.createScheduledJob.mock.calls[0][0];
    expect(sent.kind).toBe('command');
    expect(sent.command).toBe('istota-skill kv list');
    expect(sent.prompt).toBeUndefined();
  });

  it('with the file unreadable there is no New job, and ?job=new is the list', async () => {
    __history.reset(`${LIST}?job=new`);
    page.url = new URL(`${LIST}?job=new`, 'http://localhost');
    page.state = {};
    api.getScheduledJobs.mockResolvedValue(
      listing([job({ editable: false, revision: null })], { readable: false, errors: ['bad'] }),
    );
    render(Harness, { expireSession });
    expect(await screen.findByRole('table')).toBeInTheDocument();
    expect(currentUrl()).toBe(LIST);
    expect(screen.queryByRole('button', { name: 'New job' })).toBeNull();
    expect(notices.notifyWarning).not.toHaveBeenCalled();
  });

  it('a refused field stays beside its field, with the draft', async () => {
    api.createScheduledJob.mockRejectedValue(
      refused(400, 'invalid_field', 'That is not a cron schedule.', { field: 'cron' }),
    );
    open(`${LIST}?job=new`);
    await screen.findByRole('heading', { name: 'New job' });
    await fillNew();
    await save();
    expect(await screen.findByText('That is not a cron schedule.')).toBeInTheDocument();
    expect(await textbox('Schedule')).toHaveAttribute('aria-invalid', 'true');
    expect(await textbox('Name')).toHaveValue('morning');
    expect(currentUrl()).toBe(`${LIST}?job=new`);
    expect(notices.notifyError).not.toHaveBeenCalled();
  });

  it('a taken name goes beside the name', async () => {
    api.createScheduledJob.mockRejectedValue(
      refused(409, 'name_taken', 'Another job has that name.', { field: 'name' }),
    );
    open(`${LIST}?job=new`);
    await screen.findByRole('heading', { name: 'New job' });
    await fillNew();
    await save();
    expect(await screen.findByText('Another job has that name.')).toBeInTheDocument();
    expect(await textbox('Name')).toHaveAttribute('aria-invalid', 'true');
  });

  it('what the parser says goes in an alert in the form', async () => {
    api.createScheduledJob.mockRejectedValue(
      refused(422, 'invalid', 'invalid', { errors: ['This job has an unknown target.'] }),
    );
    open(`${LIST}?job=new`);
    await screen.findByRole('heading', { name: 'New job' });
    await fillNew();
    await save();
    expect(await screen.findByText('This job has an unknown target.')).toBeInTheDocument();
    expect(notices.notifyError).not.toHaveBeenCalled();
  });

  it.each([
    ['file_unreadable', 'CRON.md cannot be changed until every job in it loads.'],
    ['locked', 'CRON.md is being changed. Try again in a moment.'],
  ])('%s is a notice and the draft stays', async (code, detail) => {
    api.createScheduledJob.mockRejectedValue(refused(409, code, detail));
    open(`${LIST}?job=new`);
    await screen.findByRole('heading', { name: 'New job' });
    await fillNew();
    await save();
    await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith(detail));
    expect(await textbox('Name')).toHaveValue('morning');
  });

  it('a new job on Talk needs a room before anything is sent', async () => {
    const talkOnly = listing([]);
    talkOnly.options.outputs = ['talk'];
    __history.reset(`${LIST}?job=new`);
    page.url = new URL(`${LIST}?job=new`, 'http://localhost');
    page.state = {};
    api.getScheduledJobs.mockResolvedValue(talkOnly);
    render(Harness, { expireSession });
    await screen.findByRole('heading', { name: 'New job' });
    await fillNew();
    await save();
    expect(await screen.findByText('Choose a Talk room.')).toBeInTheDocument();
    expect(api.createScheduledJob).not.toHaveBeenCalled();
  });

  it('an unchanged bare Talk target is not judged or sent', async () => {
    open(`${LIST}?job=1`, job({ definition: { ...job().definition, target: 'talk', room: '' } }));
    await edit('Name', 'renamed');
    await save();
    await waitFor(() => expect(api.updateScheduledJob).toHaveBeenCalled());
    expect(api.updateScheduledJob.mock.calls[0][1].target).toBeUndefined();
  });

  it('an edit sends only what changed, with the revision it read', async () => {
    open(`${LIST}?job=1`, job());
    await edit('Schedule', '0 8 * * *');
    await save();
    await waitFor(() =>
      expect(api.updateScheduledJob).toHaveBeenCalledWith(1, {
        cron: '0 8 * * *',
        revision: 'r1',
      }),
    );
    await waitFor(() => expect(currentUrl()).toBe(LIST));
  });

  it('Save waits for an edit', async () => {
    open(`${LIST}?job=1`, job());
    expect(await screen.findByRole('button', { name: 'Save' })).toBeDisabled();
    await edit('Schedule', '0 8 * * *');
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
  });

  it('a changed flag is sent and a stored target that fits no control is left alone', async () => {
    open(
      `${LIST}?job=1`,
      job({ definition: { ...job().definition, target: 'web:rm1,talk:tk1', room: 'rm1' } }),
    );
    await fireEvent.click(await screen.findByRole('checkbox', { name: 'One time' }));
    await save();
    await waitFor(() =>
      expect(api.updateScheduledJob).toHaveBeenCalledWith(1, { once: true, revision: 'r1' }),
    );
  });

  it('a prompt file is named, and unchanged text is not sent', async () => {
    open(`${LIST}?job=1`, job({ definition: { ...job().definition, prompt_file: 'p/a.md' } }));
    expect(await screen.findByText(/p\/a\.md/)).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('checkbox', { name: 'One time' }));
    await save();
    await waitFor(() => expect(api.updateScheduledJob).toHaveBeenCalled());
    expect(api.updateScheduledJob.mock.calls[0][1].prompt).toBeUndefined();
  });

  it('a job that changed keeps the draft, warns, and the next save takes the new revision', async () => {
    open(`${LIST}?job=1`, job());
    await edit('Schedule', '0 8 * * *');
    api.updateScheduledJob.mockRejectedValueOnce(
      refused(409, 'job_changed', 'This job changed since the page read it.'),
    );
    api.getScheduledJobs.mockResolvedValue(listing([job({ revision: 'r2' })]));
    await save();
    await waitFor(() => expect(notices.notifyWarning).toHaveBeenCalled());
    await waitFor(() => expect(api.getScheduledJobs).toHaveBeenCalledTimes(2));
    expect(await textbox('Schedule')).toHaveValue('0 8 * * *');
    expect(currentUrl()).toBe(`${LIST}?job=1`);
    await save();
    await waitFor(() =>
      expect(api.updateScheduledJob).toHaveBeenLastCalledWith(1, {
        cron: '0 8 * * *',
        revision: 'r2',
      }),
    );
  });

  it('a reload does not move the revision under an open edit', async () => {
    open(`${LIST}?job=1`, job());
    await edit('Schedule', '0 8 * * *');
    api.getScheduledJobs.mockResolvedValue(listing([job({ revision: 'r2' })]));
    await fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(api.getScheduledJobs).toHaveBeenCalledTimes(2));
    await save();
    await waitFor(() =>
      expect(api.updateScheduledJob).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ revision: 'r1' }),
      ),
    );
  });

  it('a form with no edits follows a reload', async () => {
    open(`${LIST}?job=1`, job());
    await textbox('Name');
    api.getScheduledJobs.mockResolvedValue(
      listing([job({ cron: '5 5 * * *', enabled: false, state: 'disabled', revision: 'r2' })]),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(async () => expect(await textbox('Schedule')).toHaveValue('5 5 * * *'));
    expect(screen.getByRole('checkbox', { name: 'Enabled' })).not.toBeChecked();
  });

  it('a job gone under the form falls back to the list', async () => {
    open(`${LIST}?job=1`, job());
    await edit('Schedule', '0 8 * * *');
    api.updateScheduledJob.mockRejectedValueOnce(refused(404, 'not_found', 'No such item.'));
    api.getScheduledJobs.mockResolvedValue(listing([]));
    await save();
    await waitFor(() => expect(currentUrl()).toBe(LIST));
    expect(notices.notifyError).not.toHaveBeenCalled();
  });

  describe('changing the text of an approved job', () => {
    it.each([
      ['open', 'Keep Full access'],
      ['public', 'Keep Public'],
    ])('asks whether to keep %s, and sends it', async (state, label) => {
      api.updateScheduledJob.mockResolvedValue({ applies: 'next_sync', access_kept: true });
      open(`${LIST}?job=1`, approvedJob(state));
      await edit('Prompt', 'Read the news slowly.');
      await save();
      const dialog = within(await screen.findByRole('dialog'));
      expect(api.updateScheduledJob).not.toHaveBeenCalled();
      await fireEvent.click(dialog.getByRole('button', { name: label }));
      await waitFor(() =>
        expect(api.updateScheduledJob).toHaveBeenCalledWith(1, {
          kind: 'prompt',
          prompt: 'Read the news slowly.',
          keep_access: state,
          revision: 'r1',
        }),
      );
      await waitFor(() => expect(currentUrl()).toBe(LIST));
      expect(notices.notifyWarning).not.toHaveBeenCalled();
    });

    it('can save and run limited instead', async () => {
      open(`${LIST}?job=1`, approvedJob());
      await edit('Prompt', 'Read the news slowly.');
      await save();
      const dialog = within(await screen.findByRole('dialog'));
      await fireEvent.click(dialog.getByRole('button', { name: 'Save and run limited' }));
      await waitFor(() => expect(api.updateScheduledJob).toHaveBeenCalled());
      expect(api.updateScheduledJob.mock.calls[0][1].keep_access).toBeUndefined();
    });

    it('Cancel saves nothing and keeps the draft', async () => {
      open(`${LIST}?job=1`, approvedJob());
      await edit('Prompt', 'Read the news slowly.');
      await save();
      const dialog = within(await screen.findByRole('dialog'));
      await fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(api.updateScheduledJob).not.toHaveBeenCalled();
      expect(await textbox('Prompt')).toHaveValue('Read the news slowly.');
    });

    it('warns when the save landed and the approval did not', async () => {
      api.updateScheduledJob.mockResolvedValue({ applies: 'next_sync', access_kept: false });
      open(`${LIST}?job=1`, approvedJob());
      await edit('Prompt', 'Read the news slowly.');
      await save();
      const dialog = within(await screen.findByRole('dialog'));
      await fireEvent.click(dialog.getByRole('button', { name: 'Keep Full access' }));
      await waitFor(() =>
        expect(notices.notifyWarning).toHaveBeenCalledWith(expect.stringMatching(/approval/)),
      );
    });

    it('a field refused from the dialog goes beside the field', async () => {
      api.updateScheduledJob.mockRejectedValue(
        refused(400, 'invalid_field', 'A prompt is at most 48 KiB.', { field: 'prompt' }),
      );
      open(`${LIST}?job=1`, approvedJob());
      await edit('Prompt', 'Read the news slowly.');
      await save();
      const dialog = within(await screen.findByRole('dialog'));
      await fireEvent.click(dialog.getByRole('button', { name: 'Keep Full access' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(await textbox('Prompt')).toHaveAttribute('aria-invalid', 'true');
    });

    it('does not ask for a change the digest does not cover', async () => {
      open(`${LIST}?job=1`, approvedJob());
      await edit('Schedule', '0 8 * * *');
      await edit('Prompt', '  Read the news and summarise it.  ');
      await save();
      await waitFor(() => expect(api.updateScheduledJob).toHaveBeenCalled());
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(api.updateScheduledJob.mock.calls[0][1].keep_access).toBeUndefined();
    });

    it('does not ask a limited job, or one full only by default', async () => {
      open(
        `${LIST}?job=1`,
        job({ network: network('digest', { state: 'limited', by_default: 'open' }) }),
      );
      await edit('Prompt', 'Read the news slowly.');
      await save();
      await waitFor(() => expect(api.updateScheduledJob).toHaveBeenCalled());
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  describe('renaming', () => {
    it.each([
      ['hosts', { hosts: ['news.example.com'] }],
      ['skills', { skills: ['browse'] }],
      ['an approval', { state: 'open', approvals: [`open:${'a'.repeat(64)}`] }],
    ])('a job with %s warns its access starts over', async (_what, subject) => {
      open(`${LIST}?job=1`, job({ network: network('digest', subject) }));
      await edit('Name', 'renamed');
      await edit('Prompt', 'New text too.');
      await save();
      const dialog = within(await screen.findByRole('dialog'));
      expect(dialog.getByText(/starts over/)).toBeInTheDocument();
      expect(dialog.queryByRole('button', { name: /^Keep/ })).toBeNull();
      expect(api.updateScheduledJob).not.toHaveBeenCalled();
      await fireEvent.click(dialog.getByRole('button', { name: 'Rename and save' }));
      await waitFor(() => expect(api.updateScheduledJob).toHaveBeenCalled());
      const sent = api.updateScheduledJob.mock.calls[0][1];
      expect(sent.name).toBe('renamed');
      expect(sent.keep_access).toBeUndefined();
    });

    it('a job with nothing to lose just saves', async () => {
      open(`${LIST}?job=1`, job());
      await edit('Name', 'renamed');
      await save();
      await waitFor(() => expect(api.updateScheduledJob).toHaveBeenCalled());
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  describe('the wait for the next sync', () => {
    const pendingRow = job({ id: null, pending: true, name: 'morning', network: null });

    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    async function created() {
      open(`${LIST}?job=new`);
      await screen.findByRole('heading', { name: 'New job' });
      await fillNew();
      api.getScheduledJobs.mockResolvedValue(listing([pendingRow]));
      await save();
      await screen.findByText('Waiting for the next sync');
      return api.getScheduledJobs.mock.calls.length;
    }

    it('reads every five seconds and stops once nothing is pending', async () => {
      const reads = await created();
      await vi.advanceTimersByTimeAsync(4_000);
      expect(api.getScheduledJobs).toHaveBeenCalledTimes(reads);
      await vi.advanceTimersByTimeAsync(1_000);
      expect(api.getScheduledJobs).toHaveBeenCalledTimes(reads + 1);

      api.getScheduledJobs.mockResolvedValue(listing([job({ name: 'morning' })]));
      await vi.advanceTimersByTimeAsync(5_000);
      expect(api.getScheduledJobs).toHaveBeenCalledTimes(reads + 2);
      await screen.findByRole('link', { name: 'morning' });
      await vi.advanceTimersByTimeAsync(30_000);
      expect(api.getScheduledJobs).toHaveBeenCalledTimes(reads + 2);
    });

    it('holds one read at a time', async () => {
      const reads = await created();
      let answer = (_: unknown) => {};
      api.getScheduledJobs.mockImplementation(() => new Promise((r) => (answer = r)));
      await vi.advanceTimersByTimeAsync(20_000);
      expect(api.getScheduledJobs).toHaveBeenCalledTimes(reads + 1);
      answer(listing([job({ name: 'morning' })]));
    });

    it('gives up after two minutes and says so', async () => {
      const reads = await created();
      await vi.advanceTimersByTimeAsync(110_000);
      expect(screen.queryByText('This is taking longer than usual.')).toBeNull();
      await vi.advanceTimersByTimeAsync(15_000);
      expect(await screen.findByText('This is taking longer than usual.')).toBeInTheDocument();
      const after = api.getScheduledJobs.mock.calls.length;
      expect(after).toBeLessThanOrEqual(reads + 25);
      await vi.advanceTimersByTimeAsync(60_000);
      expect(api.getScheduledJobs).toHaveBeenCalledTimes(after);
    });

    it('a failed read does not interrupt with a notice', async () => {
      await created();
      api.getScheduledJobs.mockRejectedValueOnce(new Error('nope'));
      await vi.advanceTimersByTimeAsync(5_000);
      expect(notices.notifyError).not.toHaveBeenCalled();
    });

    it('stops when the page goes', async () => {
      const reads = await created();
      cleanup();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(api.getScheduledJobs).toHaveBeenCalledTimes(reads);
    });
  });
});
