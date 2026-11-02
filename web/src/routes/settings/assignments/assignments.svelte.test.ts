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
const visibility = vi.hoisted(() => ({ fire: () => {} }));
vi.mock('@minot/ui/ui/visibility', () => ({
  onDocumentVisible: (fn: () => void) => {
    visibility.fire = fn;
    return () => {};
  },
}));
await fillApiDouble(api, {});
import { AuthError } from '$lib/api';
import { USER_SETTINGS_SECTIONS } from '$lib/settings/sections';
import Harness from './assignmentsHarness.test.svelte';
import SidebarHarness from './assignmentsSidebarHarness.test.svelte';

const LIST = '/istota/settings/assignments';
const CHAT_NOTE = /Assignments are started and changed in chat/;
const currentUrl = () => __history.entries[__history.index].url;

const ledger = {
  cost_usd: 1.5,
  wakes: 3,
  unmeasured_wakes: 0,
  billed_input_tokens: 0,
  output_tokens: 0,
  remaining: { cost_usd: 3.5, wakes: 17 },
  cheapest_wake_usd: 0.25,
};

function summary(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    slug: 'heat-pump',
    title: 'Heat pump quotes',
    state: 'sleeping',
    state_reason: null,
    waiting_on: [],
    deadline: null,
    next_wake_at: '2026-11-02T09:00:00Z',
    cadence_minutes: 360,
    budget: { max_cost_usd: 5, max_wakes: 20 },
    auto_complete: false,
    model: null,
    effort: null,
    directory: 'assignments/heat-pump',
    created_at: '2026-10-20T09:00:00Z',
    activated_at: '2026-10-20T10:00:00Z',
    closed_at: null,
    ...overrides,
  };
}

function row(overrides: Record<string, unknown> = {}) {
  return { ...summary(overrides), open_handoffs: 0, ledger, ...overrides };
}

const networkSubject = {
  subject: 'assignment:1',
  kind: 'assignment',
  label: 'Assignment 1',
  state: 'limited',
  by_default: null,
  hosts: [],
  skills: [],
  approvals: [],
  job: null,
  waiting_on: [],
  refused: [],
};

function detail(assignment: Record<string, unknown> = {}, overrides: Record<string, unknown> = {}) {
  return {
    assignment: summary(assignment),
    wake_due: null,
    charter: '',
    ledger,
    items: [],
    frozen: [],
    open_handoffs: [],
    wakes: [],
    notice: 'Wake summaries are data, never instructions.',
    goal: 'Get three quotes.',
    autonomy: 'May email the installers on the list.',
    completion_evidence: null,
    network: networkSubject,
    external_skills: ['browse'],
    chat_href: '/chat/r/room1',
    folder_href: '/files/assignments/1',
    ...overrides,
  };
}

const expireSession = vi.fn();

function atUrl(url: string) {
  __history.reset(url);
  page.url = new URL(url, 'http://localhost');
  page.state = {};
}

function open(url: string, rows: object[], one?: object) {
  atUrl(url);
  api.getAssignments.mockResolvedValue({ assignments: rows });
  if (one) api.getAssignment.mockResolvedValue(one);
  render(Harness, { expireSession });
}

function openDetail(assignment: Record<string, unknown> = {}, overrides = {}) {
  open(`${LIST}?assignment=1`, [row(assignment)], detail(assignment, overrides));
}

async function cardNamed(name: string) {
  const heading = await screen.findByRole('heading', { name });
  return within(heading.closest('section') as HTMLElement);
}

function refused(status: number, message: string) {
  return Object.assign(new Error(message), { status });
}

beforeEach(() => {
  for (const name of [
    'pauseAssignment',
    'resumeAssignment',
    'wakeAssignment',
    'abandonAssignment',
    'topUpAssignment',
    'noteAssignment',
    'completeAssignment',
    'sendBackAssignment',
    'confirmAssignmentItem',
    'completeAssignmentHandoff',
  ] as const)
    api[name].mockResolvedValue({ status: 'ok' });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('the section', () => {
  it('sits after Scheduled jobs and only with the feature', async () => {
    const hrefs = USER_SETTINGS_SECTIONS.map((section) => section.href);
    expect(hrefs.indexOf('/assignments')).toBe(hrefs.indexOf('/jobs') + 1);

    api.getProfile.mockResolvedValue({ profile: null });
    api.getModules.mockResolvedValue({ modules: [], skills: [] });
    render(SidebarHarness, { features: { assignments: false } });
    expect(await screen.findByRole('link', { name: 'Scheduled jobs' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Assignments' })).toBeNull();
    cleanup();

    render(SidebarHarness, { features: { assignments: true } });
    expect(await screen.findByRole('link', { name: 'Assignments' })).toBeInTheDocument();
  });
});

describe('the list', () => {
  it('always carries the chat note, on the list, the empty list and the detail', async () => {
    open(LIST, [row()]);
    expect(await screen.findByText(CHAT_NOTE)).toBeInTheDocument();
    cleanup();

    open(LIST, []);
    expect(await screen.findByText(CHAT_NOTE)).toBeInTheDocument();
    cleanup();

    openDetail();
    expect(await screen.findByRole('heading', { name: 'Now' })).toBeInTheDocument();
    expect(screen.getByText(CHAT_NOTE)).toBeInTheDocument();
  });

  it('shows state, reason, what it waits on, budget and open hand-offs', async () => {
    open(LIST, [
      row(),
      row({ id: 2, title: 'Paused one', state: 'paused', state_reason: 'stalled' }),
      row({
        id: 3,
        title: 'Waiting one',
        state: 'waiting',
        waiting_on: ['user'],
        open_handoffs: 2,
      }),
      row({ id: 4, title: 'A draft', state: 'draft', next_wake_at: null }),
    ]);
    const first = within(
      (await screen.findByRole('link', { name: 'Heat pump quotes' })).closest('li')!,
    );
    expect(first.getByText('Sleeping')).toBeInTheDocument();
    expect(first.getByRole('meter', { name: 'Budget used' })).toHaveAttribute(
      'aria-valuenow',
      '30',
    );
    expect(first.getByRole('link', { name: 'Heat pump quotes' })).toHaveAttribute(
      'href',
      `${LIST}?assignment=1`,
    );
    const paused = within(screen.getByRole('link', { name: 'Paused one' }).closest('li')!);
    expect(paused.getByText('Paused')).toBeInTheDocument();
    expect(paused.getByText(/no progress/i)).toBeInTheDocument();
    const waiting = within(screen.getByRole('link', { name: 'Waiting one' }).closest('li')!);
    expect(waiting.getByText(/Waiting on you/)).toBeInTheDocument();
    expect(waiting.getByText('2 steps for you')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'A draft' })).toBeInTheDocument();
  });

  it('folds finished assignments away in a closed disclosure', async () => {
    open(LIST, [
      row(),
      row({ id: 2, title: 'Done one', state: 'completed', closed_at: '2026-10-25T09:00:00Z' }),
      row({ id: 3, title: 'Dropped one', state: 'abandoned', state_reason: 'Too dear' }),
    ]);
    const summaryLine = await screen.findByText('Finished (2)');
    const folded = summaryLine.closest('details') as HTMLDetailsElement;
    expect(folded.open).toBe(false);
    expect(within(folded).getByRole('link', { name: 'Done one' })).toBeInTheDocument();
    expect(within(folded).getByText('Too dear')).toBeInTheDocument();
  });

  it('is empty with the same words and a way to chat', async () => {
    open(LIST, []);
    expect(await screen.findByText(/no assignments yet. Start one in chat/)).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Open chat' });
    expect(link).toHaveAttribute('href', '/istota/chat');
  });

  it('is a plain state, not a failure, for an owner the gate turns away', async () => {
    atUrl(`${LIST}?assignment=1`);
    api.getAssignments.mockRejectedValue(refused(404, 'There is no such item.'));
    render(Harness, { expireSession });
    expect(await screen.findByText(/not available on your account/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(screen.queryByText('There is no such item.')).toBeNull();
    expect(notices.notifyError).not.toHaveBeenCalled();
    expect(api.getAssignment).not.toHaveBeenCalled();
  });

  it('takes the whole pane for any other load failure, with Retry', async () => {
    atUrl(LIST);
    api.getAssignments.mockRejectedValueOnce(new Error('It broke.'));
    api.getAssignments.mockResolvedValue({ assignments: [row()] });
    render(Harness, { expireSession });
    expect(await screen.findByText('It broke.')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('link', { name: 'Heat pump quotes' })).toBeInTheDocument();
  });

  it('expires the session on an auth failure', async () => {
    atUrl(LIST);
    api.getAssignments.mockRejectedValue(new AuthError());
    render(Harness, { expireSession });
    await waitFor(() => expect(expireSession).toHaveBeenCalled());
  });

  it('refreshes when the user comes back to the tab', async () => {
    open(LIST, [row()]);
    await screen.findByRole('link', { name: 'Heat pump quotes' });
    api.getAssignments.mockResolvedValue({
      assignments: [row({ title: 'Renamed in chat' })],
    });
    visibility.fire();
    expect(await screen.findByRole('link', { name: 'Renamed in chat' })).toBeInTheDocument();
  });
});

describe('the detail', () => {
  it('opens from the bell link, with a back link to the list', async () => {
    openDetail();
    expect(await screen.findByRole('heading', { name: 'Heat pump quotes' })).toBeInTheDocument();
    expect(api.getAssignment).toHaveBeenCalledWith(1);
    expect(screen.getByRole('link', { name: 'Assignments' })).toHaveAttribute('href', LIST);
  });

  it('falls back to the list with a notice when the assignment is not there', async () => {
    api.getAssignment.mockRejectedValue(refused(404, 'There is no such item.'));
    open(`${LIST}?assignment=9`, [row()]);
    expect(await screen.findByRole('link', { name: 'Heat pump quotes' })).toBeInTheDocument();
    await waitFor(() =>
      expect(notices.notifyWarning).toHaveBeenCalledWith('This assignment is not available.'),
    );
    expect(currentUrl()).toBe(LIST);
  });

  it('offers the buttons each state allows', async () => {
    const names = () =>
      ['Pause', 'Resume', 'Wake now', 'Abandon', 'Complete', 'Send back'].filter((name) =>
        screen.queryByRole('button', { name }),
      );
    const cases: [Record<string, unknown>, string[]][] = [
      [{ state: 'sleeping' }, ['Pause', 'Wake now', 'Abandon']],
      [{ state: 'active' }, ['Pause', 'Abandon']],
      [{ state: 'waiting', waiting_on: ['user'] }, ['Pause', 'Wake now', 'Abandon']],
      [
        { state: 'waiting', waiting_on: ['completion'] },
        ['Pause', 'Abandon', 'Complete', 'Send back'],
      ],
      [{ state: 'paused', state_reason: 'user' }, ['Resume', 'Abandon']],
      [{ state: 'paused', state_reason: 'budget' }, ['Abandon']],
      [{ state: 'draft' }, []],
      [{ state: 'completed' }, []],
      [{ state: 'abandoned' }, []],
    ];
    for (const [assignment, expected] of cases) {
      openDetail(assignment);
      await screen.findByRole('heading', { name: 'Now' });
      expect(names(), JSON.stringify(assignment)).toEqual(expected);
      cleanup();
    }
  });

  it('sends a draft to chat and offers no steering', async () => {
    openDetail({ state: 'draft' });
    const now = await cardNamed('Now');
    expect(now.getByText(/Continue this in chat to activate it/)).toBeInTheDocument();
    expect(now.getByRole('link', { name: 'Open chat' })).toHaveAttribute(
      'href',
      '/istota/chat/r/room1',
    );
    expect(screen.queryByRole('heading', { name: 'Note' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Top up' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Network access' })).toBeInTheDocument();
  });

  it('runs an action and refreshes', async () => {
    openDetail();
    await fireEvent.click(await screen.findByRole('button', { name: 'Pause' }));
    await waitFor(() => expect(api.pauseAssignment).toHaveBeenCalledWith(1));
    await waitFor(() => expect(api.getAssignment).toHaveBeenCalledTimes(2));
  });

  it('wakes and resumes', async () => {
    openDetail();
    await fireEvent.click(await screen.findByRole('button', { name: 'Wake now' }));
    await waitFor(() => expect(api.wakeAssignment).toHaveBeenCalledWith(1));
    cleanup();
    openDetail({ state: 'paused', state_reason: 'user' });
    await fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(api.resumeAssignment).toHaveBeenCalledWith(1));
  });

  it('reports a refused action, and refreshes to the state that refused it', async () => {
    openDetail();
    const button = await screen.findByRole('button', { name: 'Pause' });
    api.pauseAssignment.mockRejectedValue(
      refused(409, 'cannot pause an assignment that is completed'),
    );
    api.getAssignment.mockResolvedValue(detail({ state: 'completed' }, { network: null }));
    await fireEvent.click(button);
    await waitFor(() =>
      expect(notices.notifyError).toHaveBeenCalledWith(
        'cannot pause an assignment that is completed',
      ),
    );
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull());
  });

  it('returns to the list when an action finds the assignment gone', async () => {
    openDetail();
    const button = await screen.findByRole('button', { name: 'Pause' });
    api.pauseAssignment.mockRejectedValue(refused(404, 'There is no such item.'));
    api.getAssignment.mockRejectedValue(refused(404, 'There is no such item.'));
    await fireEvent.click(button);
    await waitFor(() => expect(currentUrl()).toBe(LIST));
    expect(notices.notifyError).not.toHaveBeenCalled();
    expect(notices.notifyWarning).toHaveBeenCalledWith('This assignment is not available.');
  });

  it('abandons through a dialog with a reason', async () => {
    openDetail();
    await fireEvent.click(await screen.findByRole('button', { name: 'Abandon' }));
    const dialog = within(await screen.findByRole('dialog'));
    await fireEvent.input(dialog.getByLabelText(/Reason/), { target: { value: 'Too dear' } });
    await fireEvent.click(dialog.getByRole('button', { name: 'Abandon' }));
    await waitFor(() => expect(api.abandonAssignment).toHaveBeenCalledWith(1, 'Too dear'));
  });

  it('answers a proposed completion, showing its evidence as text', async () => {
    openDetail(
      { state: 'waiting', waiting_on: ['completion'] },
      { completion_evidence: 'All three **quotes** are in.' },
    );
    expect(await screen.findByText('All three **quotes** are in.')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Complete' }));
    await waitFor(() => expect(api.completeAssignment).toHaveBeenCalledWith(1));
  });

  it('sends a completion back only with text', async () => {
    openDetail({ state: 'waiting', waiting_on: ['completion'] });
    const send = await screen.findByRole('button', { name: 'Send back' });
    expect(send).toBeDisabled();
    await fireEvent.input(screen.getByLabelText('What is missing'), {
      target: { value: 'The third quote.' },
    });
    expect(send).not.toBeDisabled();
    await fireEvent.click(send);
    await waitFor(() => expect(api.sendBackAssignment).toHaveBeenCalledWith(1, 'The third quote.'));
  });

  it('marks a hand-off done with a report, and keeps a refusal beside its field', async () => {
    openDetail(
      {},
      {
        open_handoffs: [
          {
            id: 7,
            text: 'Photograph the label',
            report: 'The model number',
            due_at: '2026-11-05T00:00:00Z',
            created_at: '2026-11-01T00:00:00Z',
          },
        ],
      },
    );
    const card = await cardNamed('Hand-offs');
    expect(card.getByText('Photograph the label')).toBeInTheDocument();
    expect(card.getByText(/The model number/)).toBeInTheDocument();
    const done = card.getByRole('button', { name: 'Mark done' });
    await fireEvent.input(card.getByLabelText('Your report'), { target: { value: 'XK-200' } });
    api.completeAssignmentHandoff.mockRejectedValueOnce(refused(400, 'the report is too long'));
    await fireEvent.click(done);
    expect(await card.findByText('the report is too long')).toBeInTheDocument();
    expect(notices.notifyError).not.toHaveBeenCalled();
    await fireEvent.click(done);
    await waitFor(() =>
      expect(api.completeAssignmentHandoff).toHaveBeenLastCalledWith(1, 7, 'XK-200'),
    );
  });

  it('offers Confirm only on an open confirmed item', async () => {
    const item = { detail: null, checked_at: null };
    openDetail(
      {},
      {
        items: [
          {
            ...item,
            id: 1,
            position: 1,
            kind: 'mechanical',
            text: 'quotes.csv parses',
            status: 'passing',
          },
          { ...item, id: 2, position: 2, kind: 'confirmed', text: 'You agree', status: 'open' },
          { ...item, id: 3, position: 3, kind: 'confirmed', text: 'Signed', status: 'confirmed' },
        ],
      },
    );
    const card = await cardNamed('Done when');
    const confirms = card.getAllByRole('button', { name: /^Confirm/ });
    expect(confirms).toHaveLength(1);
    expect(card.getByText('Passing')).toBeInTheDocument();
    await fireEvent.click(confirms[0]);
    await waitFor(() => expect(api.confirmAssignmentItem).toHaveBeenCalledWith(1, 2));
  });

  it('shows the budget in list price with unmeasured wakes, and tops up', async () => {
    openDetail(
      { state: 'paused', state_reason: 'budget' },
      { ledger: { ...ledger, unmeasured_wakes: 2 } },
    );
    const card = await cardNamed('Budget');
    expect(card.getByText(/list price/)).toBeInTheDocument();
    expect(card.getByText(/2 wakes could not be measured/)).toBeInTheDocument();
    expect(card.getByRole('meter', { name: 'Wakes used' })).toHaveAttribute('aria-valuenow', '3');
    api.topUpAssignment.mockResolvedValue({
      status: 'ok',
      still_paused: { remaining: { cost_usd: 0.1, wakes: 4 }, cheapest_wake_usd: 0.25 },
    });
    await fireEvent.input(card.getByLabelText('Dollars to add'), { target: { value: '1.5' } });
    await fireEvent.click(card.getByRole('button', { name: 'Top up' }));
    await waitFor(() => expect(api.topUpAssignment).toHaveBeenCalledWith(1, { usd: 1.5 }));
    expect(await card.findByText(/still paused/i)).toBeInTheDocument();
    expect(card.getByText(/\$0\.25/)).toBeInTheDocument();
  });

  it('tops up in wakes, and keeps a ceiling refusal beside the field', async () => {
    openDetail();
    const card = await cardNamed('Budget');
    await fireEvent.click(card.getByRole('radio', { name: 'Wakes' }));
    await fireEvent.input(card.getByLabelText('Wakes to add'), { target: { value: '5' } });
    api.topUpAssignment.mockRejectedValueOnce(refused(400, 'over the ceiling'));
    await fireEvent.click(card.getByRole('button', { name: 'Top up' }));
    await waitFor(() => expect(api.topUpAssignment).toHaveBeenCalledWith(1, { wakes: 5 }));
    expect(await card.findByText('over the ceiling')).toBeInTheDocument();
  });

  it('sends a note, saying the next wake reads it as the user words', async () => {
    openDetail();
    const card = await cardNamed('Note');
    expect(card.getByText(/next wake reads it as your words/)).toBeInTheDocument();
    const send = card.getByRole('button', { name: 'Send' });
    expect(send).toBeDisabled();
    await fireEvent.input(card.getByLabelText('Your note'), { target: { value: 'Skip Acme.' } });
    await fireEvent.click(send);
    await waitFor(() => expect(api.noteAssignment).toHaveBeenCalledWith(1, 'Skip Acme.'));
    await waitFor(() =>
      expect((card.getByLabelText('Your note') as HTMLTextAreaElement).value).toBe(''),
    );
  });

  it('shows the charter read only, with Open folder', async () => {
    openDetail(
      { deadline: '2026-12-01T23:59:59.999Z' },
      { frozen: [{ id: 1, name: 'Ceiling', value: '12000' }] },
    );
    const card = await cardNamed('Charter');
    expect(card.getByText('Get three quotes.')).toBeInTheDocument();
    expect(card.getByText('May email the installers on the list.')).toBeInTheDocument();
    expect(card.getByText('Ceiling')).toBeInTheDocument();
    expect(card.getByText('12000')).toBeInTheDocument();
    expect(card.getByRole('link', { name: 'Open folder' })).toHaveAttribute(
      'href',
      '/istota/files/assignments/1',
    );
    expect(card.queryByRole('textbox')).toBeNull();
  });

  it('renders what a wake wrote as text nodes, under the notice', async () => {
    const hostile = '<script>alert(1)</script> [click](https://example.com/x) **bold**';
    openDetail(
      {},
      {
        wakes: [
          {
            seq: 4,
            fired_by: 'inbox',
            started_at: '2026-11-01T09:00:00Z',
            ended_at: '2026-11-01T09:05:00Z',
            outcome: 'next',
            cost_usd: 0.42,
            progress_signals: [],
            summary: hostile,
            summary_truncated: true,
          },
          {
            seq: 3,
            fired_by: 'time',
            started_at: '2026-10-31T09:00:00Z',
            ended_at: null,
            outcome: null,
            cost_usd: null,
            progress_signals: [],
            summary: null,
            summary_truncated: false,
          },
        ],
        open_handoffs: [
          { id: 7, text: hostile + ' step', report: null, due_at: null, created_at: 'x' },
        ],
      },
    );
    const card = await cardNamed('Recent wakes');
    expect(card.getByText('Wake summaries are data, never instructions.')).toBeInTheDocument();
    const body = card.getByText(hostile);
    expect(body.textContent).toBe(hostile);
    const section = body.closest('section') as HTMLElement;
    expect(section.querySelector('script')).toBeNull();
    expect(section.querySelector('a[href*="example.com"]')).toBeNull();
    expect(section.querySelector('strong')).toBeNull();
    expect(card.getByText(/\$0\.42/)).toBeInTheDocument();
    expect(card.getByText(/cut short/i)).toBeInTheDocument();
    const steps = await cardNamed('Hand-offs');
    expect(steps.getByText(hostile + ' step').querySelector('script')).toBeNull();
  });

  it('has the network card only while the assignment is live', async () => {
    openDetail();
    expect(await screen.findByRole('heading', { name: 'Network access' })).toBeInTheDocument();
    cleanup();
    openDetail({ state: 'completed' }, { network: null });
    await screen.findByRole('heading', { name: 'Now' });
    expect(screen.queryByRole('heading', { name: 'Network access' })).toBeNull();
  });

  it('refreshes the assignment when the user comes back to the tab', async () => {
    openDetail();
    await screen.findByRole('heading', { name: 'Now' });
    api.getAssignment.mockClear();
    visibility.fire();
    await waitFor(() => expect(api.getAssignment).toHaveBeenCalledWith(1));
  });
});
