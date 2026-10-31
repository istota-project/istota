import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
const notices = vi.hoisted(() => ({ notifyError: vi.fn() }));
vi.mock('@minot/ui/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  notifyError: notices.notifyError,
}));
await fillApiDouble(api, {});
import { AuthError } from '$lib/api';
import Harness from './networkHarness.test.svelte';

const job = {
  digest: 'a'.repeat(64),
  reach: 'private',
  prompt: 'Read the news and summarise it.',
  command: '',
  skill: '',
  skill_args: '',
};

function subject(overrides = {}) {
  return {
    subject: 'cron:digest',
    kind: 'cron',
    label: 'Scheduled job: digest',
    state: 'limited',
    hosts: [],
    skills: [],
    approvals: [],
    job,
    refused: [],
    ...overrides,
  };
}

function listing(...subjects: object[]) {
  return { subjects, external_skills: ['browse', 'devbox', 'wordpress'] };
}

const expireSession = vi.fn();

beforeEach(() => {
  api.addNetworkGrant.mockResolvedValue({ ok: true });
  api.approveNetworkJob.mockResolvedValue({ ok: true });
  api.removeNetworkGrant.mockResolvedValue({ ok: true });
  api.dismissNetworkRefusal.mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function card(name = 'Scheduled job: digest') {
  const heading = await screen.findByRole('heading', { name });
  return within(heading.closest('section') as HTMLElement);
}

function show(...subjects: object[]) {
  api.getNetworkAccess.mockResolvedValue(listing(...subjects));
  render(Harness, { expireSession });
}

it('shows each state in words', async () => {
  show(
    subject({ state: 'open', approvals: ['open:' + 'a'.repeat(64)] }),
    subject({ subject: 'cron:pub', label: 'Scheduled job: pub', state: 'public', job: null }),
    subject({
      subject: 'cron:lim',
      label: 'Scheduled job: lim',
      hosts: ['news.example.com'],
      skills: ['browse'],
      job: null,
    }),
  );
  expect((await card()).getByText('Full access')).toBeInTheDocument();
  expect((await card('Scheduled job: pub')).getByText('Public')).toBeInTheDocument();
  const limited = await card('Scheduled job: lim');
  expect(limited.getByText('Limited')).toBeInTheDocument();
  expect(limited.getByText('news.example.com')).toBeInTheDocument();
  expect(limited.getByText('browse')).toBeInTheDocument();
});

it('says so when there is nothing to decide', async () => {
  show();
  expect(await screen.findByText(/Nothing unattended/)).toBeInTheDocument();
});

it('warns above a refused host, and Add goes through the route', async () => {
  show(
    subject({ refused: [{ entry: 'evil.example', hits: 2, last_seen: '2026-10-30 12:00:00' }] }),
  );
  const view = await card();
  expect(view.getByText(/a page the job reads can make it ask/i)).toBeInTheDocument();
  expect(view.getByText('evil.example')).toBeInTheDocument();
  await fireEvent.click(view.getByRole('button', { name: 'Add evil.example' }));
  await waitFor(() =>
    expect(api.addNetworkGrant).toHaveBeenCalledWith('cron:digest', 'evil.example'),
  );
  // An ordinary host asks nothing further.
  expect(screen.queryByRole('dialog')).toBeNull();
  await waitFor(() => expect(api.getNetworkAccess).toHaveBeenCalledTimes(2));
});

it('Dismiss goes through its own route', async () => {
  show(
    subject({ refused: [{ entry: 'evil.example', hits: 1, last_seen: '2026-10-30 12:00:00' }] }),
  );
  const view = await card();
  await fireEvent.click(view.getByRole('button', { name: 'Dismiss evil.example' }));
  await waitFor(() =>
    expect(api.dismissNetworkRefusal).toHaveBeenCalledWith('cron:digest', 'evil.example'),
  );
  expect(api.addNetworkGrant).not.toHaveBeenCalled();
});

it('adds a host by hand and keeps a refusal beside the field', async () => {
  api.addNetworkGrant.mockRejectedValueOnce(
    Object.assign(new Error('A host needs at least two labels.'), { field: 'entry' }),
  );
  show(subject());
  const view = await card();
  const input = view.getByLabelText('Add a host');
  await fireEvent.input(input, { target: { value: 'nope' } });
  await fireEvent.click(view.getByRole('button', { name: 'Add host' }));
  expect(await view.findByText('A host needs at least two labels.')).toBeInTheDocument();
  expect((input as HTMLInputElement).value).toBe('nope');
  expect(notices.notifyError).not.toHaveBeenCalled();

  await fireEvent.input(input, { target: { value: 'news.example.com' } });
  await fireEvent.click(view.getByRole('button', { name: 'Add host' }));
  await waitFor(() =>
    expect(api.addNetworkGrant).toHaveBeenLastCalledWith('cron:digest', 'news.example.com'),
  );
  await waitFor(() => expect((input as HTMLInputElement).value).toBe(''));
});

it('removes an entry, and a failed removal is a notice', async () => {
  api.removeNetworkGrant.mockRejectedValueOnce(new Error('That could not be saved.'));
  show(subject({ hosts: ['news.example.com'] }));
  const view = await card();
  await fireEvent.click(view.getByRole('button', { name: 'Remove news.example.com' }));
  await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('That could not be saved.'));
  await fireEvent.click(view.getByRole('button', { name: 'Remove news.example.com' }));
  await waitFor(() =>
    expect(api.removeNetworkGrant).toHaveBeenLastCalledWith('cron:digest', 'news.example.com'),
  );
});

it('removing full access narrows the job with no dialog, and offers it again', async () => {
  const entry = 'open:' + 'a'.repeat(64);
  show(subject({ state: 'open', approvals: [entry] }));
  const view = await card();
  await fireEvent.click(view.getByRole('button', { name: 'Remove full access' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  await waitFor(() => expect(api.removeNetworkGrant).toHaveBeenCalledWith('cron:digest', entry));
});

it.each([
  ['browse', /unbounded/i],
  ['devbox', /unbounded/i],
])('a grant of %s goes through a dialog that says it is unbounded', async (name, warning) => {
  show(subject({ refused: [{ entry: `skill:${name}`, hits: 1, last_seen: '2026-10-30' }] }));
  const view = await card();
  await fireEvent.click(view.getByRole('button', { name: `Add skill:${name}` }));
  const dialog = within(await screen.findByRole('dialog'));
  expect(dialog.getByText(warning)).toBeInTheDocument();
  expect(api.addNetworkGrant).not.toHaveBeenCalled();
  await fireEvent.click(dialog.getByRole('button', { name: 'Grant' }));
  await waitFor(() =>
    expect(api.addNetworkGrant).toHaveBeenCalledWith('cron:digest', `skill:${name}`),
  );
});

it('another external skill is added straight away from the list', async () => {
  show(subject());
  const view = await card();
  await fireEvent.click(view.getByText('Add a skill'));
  await fireEvent.click(view.getByRole('button', { name: 'Add skill wordpress', hidden: true }));
  await waitFor(() =>
    expect(api.addNetworkGrant).toHaveBeenCalledWith('cron:digest', 'skill:wordpress'),
  );
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('a skill already granted is not offered', async () => {
  show(subject({ skills: ['wordpress'] }));
  const view = await card();
  expect(view.queryByRole('button', { name: 'Add skill wordpress', hidden: true })).toBeNull();
  expect(view.getByRole('button', { name: 'Add skill browse', hidden: true })).toBeInTheDocument();
});

it('full access shows the whole text and sends the digest the page rendered', async () => {
  show(subject());
  const view = await card();
  await fireEvent.click(view.getByRole('button', { name: 'Approve full access' }));
  const dialog = within(await screen.findByRole('dialog'));
  expect(dialog.getByText(/unbounded/i)).toBeInTheDocument();
  expect(dialog.getByText(job.prompt)).toBeInTheDocument();
  expect(api.approveNetworkJob).not.toHaveBeenCalled();
  await fireEvent.click(dialog.getByRole('button', { name: 'Approve' }));
  await waitFor(() =>
    expect(api.approveNetworkJob).toHaveBeenCalledWith('cron:digest', 'open', job.digest),
  );
});

it('public is offered only to a job that asks, and shows its text', async () => {
  show(
    subject(),
    subject({
      subject: 'cron:asks',
      label: 'Scheduled job: asks',
      job: { ...job, reach: 'public', prompt: 'Read public pages.' },
      refused: [{ entry: 'public', hits: 1, last_seen: '2026-10-30' }],
    }),
  );
  expect((await card()).queryByRole('button', { name: 'Approve public access' })).toBeNull();
  const view = await card('Scheduled job: asks');
  expect(view.getByText('This job asks for public access.')).toBeInTheDocument();
  await fireEvent.click(view.getByRole('button', { name: 'Approve public access' }));
  const dialog = within(await screen.findByRole('dialog'));
  expect(dialog.getByText('Read public pages.')).toBeInTheDocument();
  await fireEvent.click(dialog.getByRole('button', { name: 'Approve' }));
  await waitFor(() =>
    expect(api.approveNetworkJob).toHaveBeenCalledWith('cron:asks', 'public', job.digest),
  );
});

it('a refused approval stays in the dialog and the page reloads the text', async () => {
  api.approveNetworkJob.mockRejectedValueOnce(
    new Error("This job's text has changed. Reload the page and read it again."),
  );
  show(subject());
  const view = await card();
  await fireEvent.click(view.getByRole('button', { name: 'Approve full access' }));
  const dialog = within(await screen.findByRole('dialog'));
  await fireEvent.click(dialog.getByRole('button', { name: 'Approve' }));
  expect(await dialog.findByText(/text has changed/)).toBeInTheDocument();
  await waitFor(() => expect(api.getNetworkAccess).toHaveBeenCalledTimes(2));
});

it('a request from a credential verb is offered as full access', async () => {
  show(subject({ refused: [{ entry: 'open', hits: 1, last_seen: '2026-10-30' }] }));
  const view = await card();
  expect(view.getByText('This job asked for full access.')).toBeInTheDocument();
  await fireEvent.click(view.getByRole('button', { name: 'Dismiss open' }));
  await waitFor(() =>
    expect(api.dismissNetworkRefusal).toHaveBeenCalledWith('cron:digest', 'open'),
  );
});

it('a subject with no job offers neither approval', async () => {
  show(subject({ subject: 'tasks-file', kind: 'tasks-file', label: 'TASKS.md', job: null }));
  const view = await card('TASKS.md');
  expect(view.queryByRole('button', { name: 'Approve full access' })).toBeNull();
  expect(view.getByLabelText('Add a host')).toBeInTheDocument();
});

it('a name and a host are drawn as text', async () => {
  show(
    subject({
      subject: 'cron:<img src=x onerror=alert(1)>',
      label: 'Scheduled job: <img src=x onerror=alert(1)>',
      refused: [{ entry: 'evil.example', hits: 1, last_seen: '2026-10-30' }],
    }),
  );
  await screen.findByText(/Scheduled job: <img/);
  expect(document.querySelector('img[src="x"]')).toBeNull();
});

it('an expired session goes to the session handler', async () => {
  api.getNetworkAccess.mockRejectedValue(new AuthError());
  render(Harness, { expireSession });
  await waitFor(() => expect(expireSession).toHaveBeenCalled());
});

it('a failed load offers a retry', async () => {
  api.getNetworkAccess.mockRejectedValueOnce(new Error('Network access could not be loaded.'));
  api.getNetworkAccess.mockResolvedValue(listing(subject()));
  render(Harness, { expireSession });
  expect(await screen.findByText('Network access could not be loaded.')).toBeInTheDocument();
  await fireEvent.click(screen.getByRole('button', { name: /retry/i }));
  expect(await screen.findByRole('heading', { name: 'Scheduled job: digest' })).toBeInTheDocument();
});
