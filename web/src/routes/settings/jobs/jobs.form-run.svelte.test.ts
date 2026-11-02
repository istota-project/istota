// The job form's Model and run, Publish and Asks ahead sections.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { __history } from '../../../../vitest-stubs/app-navigation';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
vi.mock('$app/paths', () => ({ base: '/istota' }));
await fillApiDouble(api, {});
import { JobSaveError } from '$lib/api';
import Harness from './jobsHarness.test.svelte';

const LIST = '/istota/settings/jobs';
const FORM = `${LIST}?job=1`;

function model(ref: string, efforts = ['low', 'high']) {
  return {
    ref,
    name: ref,
    provider: ref.split('/')[0],
    family: null,
    reasoning: true,
    efforts,
    context_window: 1000,
    input: ['text'],
    price: null,
    enabled: true,
  };
}

const CHOICES = {
  model_aliases: [{ alias: 'smart', target: 'claude-opus', effort: null, efforts: ['max'] }],
  models: [model('anthropic/claude-opus'), model('openrouter/vendor/kimi', ['medium'])],
  default_model: 'claude-sonnet',
  providers: [],
};
const PROVIDERS = [
  { id: 'anthropic', kind: 'anthropic', default: true, default_model: 'claude-sonnet' },
  { id: 'openrouter', kind: 'openrouter', default: false, default_model: null },
];
const NATIVE = { kind: 'native', label: 'Native', model_namespace: 'ns-native' };
const APPROVAL = `open:${'a'.repeat(64)}`;

function network(state = 'limited') {
  return {
    subject: 'cron:digest',
    kind: 'cron',
    label: 'Scheduled job: digest',
    state,
    by_default: null,
    hosts: [],
    skills: [],
    approvals: state === 'limited' ? [] : [APPROVAL],
    job: {
      run_state: 'running',
      digest: 'a'.repeat(64),
      once: false,
      waiting: false,
      reach: 'private',
      settable: true,
      needs_approval: { public: true, open: true },
      prompt: 'Read the news.',
      command: '',
      skill: '',
      skill_args: '',
      publish_shared_kv: '',
      publish_shared_kv_trusted: false,
    },
    waiting_on: [],
    refused: [],
  };
}

function job(definition: Record<string, unknown> = {}, overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    pending: false,
    name: 'digest',
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
      prompt: 'Read the news.',
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
      ...definition,
    },
    revision: 'r1',
    editable: true,
    network: network(),
    ...overrides,
  };
}

function open(
  url: string,
  jobs: object[],
  { options = {}, isAdmin = false }: { options?: object; isAdmin?: boolean } = {},
) {
  __history.reset(url);
  page.url = new URL(url, 'http://localhost');
  page.state = {};
  api.getScheduledJobs.mockResolvedValue({
    jobs,
    file: { readable: true, errors: [] },
    options: {
      outputs: ['web'],
      rooms: { talk: [], web: [] },
      selectable_brains: [],
      can_publish: false,
      ...options,
    },
    external_skills: [],
    timezone: 'UTC',
  });
  render(Harness, { isAdmin });
}

const modelField = () => screen.findByRole('combobox', { name: 'Model' });
const textbox = (name: string) => screen.findByRole('textbox', { name });
const button = (name: string) => screen.findByRole('button', { name });
const save = async () =>
  fireEvent.click(await screen.findByRole('button', { name: /^(Save|Create job)$/ }));

async function edit(name: string, value: string) {
  await fireEvent.input(await textbox(name), { target: { value } });
}

async function pick(ariaLabel: string, optionLabel: string) {
  const trigger = await button(ariaLabel);
  await fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(trigger, { pointerType: 'mouse', button: 0 });
  await fireEvent.click(trigger);
  const item = (await screen.findByText(optionLabel)).closest('[data-select-item]');
  if (!item) throw new Error(`no option ${optionLabel} under ${ariaLabel}`);
  await fireEvent.pointerMove(item, { pointerType: 'mouse' });
  await fireEvent.pointerDown(item, { pointerType: 'mouse', button: 0 });
  await fireEvent.pointerUp(item, { pointerType: 'mouse', button: 0 });
}

async function chooseModel(label: string) {
  await fireEvent.focus(await modelField());
  await fireEvent.mouseDown(await screen.findByRole('option', { name: label }));
}

async function sent() {
  await waitFor(() => expect(api.updateScheduledJob).toHaveBeenCalled());
  return api.updateScheduledJob.mock.calls[0][1];
}

beforeEach(() => {
  api.getJobModelChoices.mockResolvedValue(CHOICES);
  api.createScheduledJob.mockResolvedValue({ applies: 'next_sync' });
  api.updateScheduledJob.mockResolvedValue({ applies: 'next_sync', access_kept: null });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('model and run', () => {
  it('asks for the choices the job runs on, and names the default', async () => {
    open(FORM, [job()]);
    const field = await modelField();
    await waitFor(() =>
      expect(field).toHaveAttribute('placeholder', 'Default model (claude-sonnet)'),
    );
    expect(api.getJobModelChoices).toHaveBeenCalledWith('', '');
  });

  it('a pick is sent as its value, and Effort offers that model what it takes', async () => {
    open(FORM, [job()]);
    await chooseModel('openrouter/vendor/kimi');
    await pick('Effort', 'medium');
    await save();
    expect(await sent()).toEqual({
      model: 'openrouter/vendor/kimi',
      effort: 'medium',
      revision: 'r1',
    });
  });

  it('is folded on a new job and with nothing set, and open with something set', async () => {
    open(FORM, [job()]);
    expect((await modelField()).closest('details')).not.toHaveAttribute('open');
    cleanup();
    open(FORM, [job({ effort: 'high' })]);
    expect((await modelField()).closest('details')).toHaveAttribute('open');
    cleanup();
    open(`${LIST}?job=new`, []);
    expect((await modelField()).closest('details')).not.toHaveAttribute('open');
  });

  it('a new job sends what was picked', async () => {
    open(`${LIST}?job=new`, []);
    await edit('Name', 'morning');
    await edit('Schedule', '0 7 * * *');
    await edit('Prompt', 'Say hi.');
    await chooseModel('smart → claude-opus');
    await save();
    await waitFor(() => expect(api.createScheduledJob).toHaveBeenCalled());
    expect(api.createScheduledJob.mock.calls[0][0].model).toBe('smart');
  });

  it('a stored model the list lacks stays, is flagged, and is sent only if touched', async () => {
    open(FORM, [job({ model: 'old/retired', effort: 'high' })]);
    expect(await modelField()).toHaveValue('old/retired');
    expect(await screen.findByText(/Not in your list/)).toBeInTheDocument();
    await edit('Schedule', '0 8 * * *');
    await save();
    expect(await sent()).toEqual({ cron: '0 8 * * *', revision: 'r1' });
  });

  it('a bare id the list holds under its provider is not flagged', async () => {
    open(FORM, [job({ model: 'claude-opus' })]);
    await waitFor(() => expect(api.getJobModelChoices).toHaveBeenCalled());
    await modelField();
    await new Promise((resolve) => setTimeout(resolve));
    expect(screen.queryByText(/Not in your list/)).toBeNull();
  });

  it('with no list a stored model is not called unlisted, and can still be typed', async () => {
    api.getJobModelChoices.mockRejectedValue(new Error('down'));
    open(FORM, [job({ model: 'old/retired' })]);
    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText(/Not in your list/)).toBeNull();
    expect(await modelField()).toHaveValue('old/retired');
  });

  it('a refused model opens the section and sits beside the field', async () => {
    api.updateScheduledJob.mockRejectedValueOnce(
      new JobSaveError('You cannot pick that model.', 400, 'invalid_field', 'model', []),
    );
    open(FORM, [job()]);
    await fireEvent.input(await modelField(), { target: { value: 'nope' } });
    await save();
    expect(await screen.findByText('You cannot pick that model.')).toBeInTheDocument();
    expect((await modelField()).closest('details')).toHaveAttribute('open');
    expect(await modelField()).toHaveValue('nope');
  });

  describe('brain and provider', () => {
    const admin = { options: { selectable_brains: [NATIVE] }, isAdmin: true };

    it('neither renders for someone who cannot set them', async () => {
      open(FORM, [job()]);
      await modelField();
      expect(screen.queryByRole('button', { name: 'Brain' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Provider' })).toBeNull();
    });

    it('changing the brain refetches and clears a model the new list lacks', async () => {
      open(FORM, [job({ model: 'anthropic/claude-opus', effort: 'high' })], admin);
      await modelField();
      api.getJobModelChoices.mockResolvedValue({
        ...CHOICES,
        models: [model('openrouter/vendor/kimi')],
      });
      await pick('Brain', 'Native');
      await waitFor(() => expect(api.getJobModelChoices).toHaveBeenLastCalledWith('native', ''));
      await waitFor(async () => expect(await modelField()).toHaveValue(''));
      await save();
      expect(await sent()).toEqual({ brain: 'native', model: '', effort: '', revision: 'r1' });
    });

    it('changing the brain keeps a model the new list holds', async () => {
      open(FORM, [job({ model: 'anthropic/claude-opus' })], admin);
      await modelField();
      await pick('Brain', 'Native');
      await waitFor(() => expect(api.getJobModelChoices).toHaveBeenCalledTimes(2));
      await save();
      expect(await sent()).toEqual({ brain: 'native', revision: 'r1' });
    });

    it('a brain whose list did not load clears nothing', async () => {
      open(FORM, [job({ model: 'anthropic/claude-opus' })], admin);
      await modelField();
      api.getJobModelChoices.mockRejectedValue(new Error('down'));
      await pick('Brain', 'Native');
      expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument();
      expect(await modelField()).toHaveValue('anthropic/claude-opus');
    });

    it('a slower answer for the brain left behind is dropped', async () => {
      let late!: (value: unknown) => void;
      api.getJobModelChoices.mockImplementationOnce(() => new Promise((res) => (late = res)));
      open(FORM, [job()], admin);
      await modelField();
      api.getJobModelChoices.mockResolvedValue({
        ...CHOICES,
        models: [model('openrouter/vendor/kimi')],
      });
      await pick('Brain', 'Native');
      await waitFor(() => expect(api.getJobModelChoices).toHaveBeenCalledTimes(2));
      late({ ...CHOICES, models: [model('anthropic/stale')] });
      await new Promise((resolve) => setTimeout(resolve));
      await fireEvent.focus(await modelField());
      expect(screen.queryByRole('option', { name: 'anthropic/stale' })).toBeNull();
      expect(await screen.findByRole('option', { name: 'openrouter/vendor/kimi' })).toBeTruthy();
    });

    it('Provider shows only when the choices list providers, and moves the list', async () => {
      api.getJobModelChoices.mockResolvedValue({ ...CHOICES, providers: PROVIDERS });
      open(FORM, [job({ model: 'anthropic/claude-opus', effort: 'high' })], admin);
      expect(await button('Provider')).toHaveTextContent('anthropic (default)');
      await pick('Provider', 'openrouter');
      await waitFor(() =>
        expect(api.getJobModelChoices).toHaveBeenLastCalledWith('', 'openrouter'),
      );
      expect(await modelField()).toHaveValue('');
      await fireEvent.focus(await modelField());
      expect(screen.queryByRole('option', { name: 'anthropic/claude-opus' })).toBeNull();
      await save();
      expect(await sent()).toEqual({
        provider: 'openrouter',
        model: '',
        effort: '',
        revision: 'r1',
      });
    });

    it("a pin since taken off the operator's list saves unchanged, with a warning", async () => {
      open(FORM, [job({ brain: 'retired-kind' })], { isAdmin: true });
      expect(await button('Brain')).toHaveTextContent('retired-kind');
      expect(await screen.findByText(/falls through/)).toBeInTheDocument();
      await edit('Schedule', '0 8 * * *');
      await save();
      expect(await sent()).toEqual({ cron: '0 8 * * *', revision: 'r1' });
    });

    it.each([
      ['brain', { brain: 'native' }, /brain setting is ignored because only an admin can set it/],
      ['provider', { provider: 'openrouter' }, /provider setting is ignored/],
      ['both', { brain: 'native', provider: 'openrouter' }, /brain and provider settings are/],
    ])('a %s the sync drops says so, and stays until removed', async (_what, stored, caption) => {
      open(FORM, [job(stored)]);
      expect(await screen.findByText(caption)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Brain' })).toBeNull();
      await edit('Schedule', '0 8 * * *');
      await save();
      expect(await sent()).toEqual({ cron: '0 8 * * *', revision: 'r1' });
    });

    it('Remove clears what the sync drops, and only that', async () => {
      open(FORM, [job({ brain: 'native', provider: 'openrouter' })]);
      await fireEvent.click(await button('Remove the brain and provider'));
      expect(screen.queryByText(/ignored because/)).toBeNull();
      await save();
      expect(await sent()).toEqual({ brain: '', provider: '', revision: 'r1' });
    });
  });
});

describe('publish', () => {
  const writer = { options: { can_publish: true } };

  it('is not there for someone who may not publish, with no target', async () => {
    open(FORM, [job()]);
    await modelField();
    expect(screen.queryByRole('textbox', { name: 'Publish to' })).toBeNull();
    expect(screen.queryByRole('checkbox', { name: 'Trusted' })).toBeNull();
  });

  it('a writer sets a target and trust, and is told what they mean', async () => {
    open(FORM, [job()], writer);
    const trusted = await screen.findByRole('checkbox', { name: 'Trusted' });
    expect(trusted).toBeDisabled();
    expect(screen.getByText(/other users' briefings/)).toBeInTheDocument();
    expect(screen.getByText(/not fenced as untrusted/)).toBeInTheDocument();
    await edit('Publish to', ' news/today ');
    await waitFor(() => expect(trusted).toBeEnabled());
    await fireEvent.click(trusted);
    await save();
    expect(await sent()).toEqual({
      publish_shared_kv: 'news/today',
      publish_shared_kv_trusted: true,
      revision: 'r1',
    });
  });

  it('emptying the target drops trust with it', async () => {
    open(FORM, [job({ publish_shared_kv: 'a/b', publish_shared_kv_trusted: true })], writer);
    await edit('Publish to', '');
    await save();
    expect(await sent()).toEqual({
      publish_shared_kv: '',
      publish_shared_kv_trusted: false,
      revision: 'r1',
    });
  });

  it('a refused target sits beside its field', async () => {
    api.updateScheduledJob.mockRejectedValueOnce(
      new JobSaveError(
        'That namespace is reserved.',
        400,
        'invalid_field',
        'publish_shared_kv',
        [],
      ),
    );
    open(FORM, [job()], writer);
    await edit('Publish to', '_doctor/x');
    await save();
    expect(await screen.findByText('That namespace is reserved.')).toBeInTheDocument();
    expect(await textbox('Publish to')).toHaveAttribute('aria-invalid', 'true');
  });

  describe('someone who may no longer publish', () => {
    const stored = { publish_shared_kv: 'a/b', publish_shared_kv_trusted: true };

    it('sees the target with Remove only, and leaving it sends nothing', async () => {
      open(FORM, [job(stored)]);
      expect(await screen.findByText('a/b')).toBeInTheDocument();
      expect(screen.queryByRole('textbox', { name: 'Publish to' })).toBeNull();
      expect(screen.queryByRole('checkbox', { name: 'Trusted' })).toBeNull();
      await edit('Schedule', '0 8 * * *');
      await save();
      expect(await sent()).toEqual({ cron: '0 8 * * *', revision: 'r1' });
    });

    it('Remove clears it', async () => {
      open(FORM, [job(stored)]);
      await fireEvent.click(await button('Remove the publish target'));
      expect(screen.queryByText('a/b')).toBeNull();
      await save();
      expect(await sent()).toEqual({
        publish_shared_kv: '',
        publish_shared_kv_trusted: false,
        revision: 'r1',
      });
    });
  });

  describe('on a job with an approval', () => {
    const approved = (definition = {}) => job(definition, { network: network('open') });

    it('a changed target asks whether to keep it, since the digest covers it', async () => {
      open(FORM, [approved()], writer);
      await edit('Publish to', 'news/today');
      await save();
      const dialog = within(await screen.findByRole('dialog'));
      expect(api.updateScheduledJob).not.toHaveBeenCalled();
      await fireEvent.click(dialog.getByRole('button', { name: 'Keep Full access' }));
      expect(await sent()).toEqual({
        publish_shared_kv: 'news/today',
        keep_access: 'open',
        revision: 'r1',
      });
    });

    it('changed trust asks too', async () => {
      open(FORM, [approved({ publish_shared_kv: 'a/b' })], writer);
      await fireEvent.click(await screen.findByRole('checkbox', { name: 'Trusted' }));
      await save();
      expect(await screen.findByRole('dialog')).toBeInTheDocument();
      expect(api.updateScheduledJob).not.toHaveBeenCalled();
    });

    it('removing a target asks too', async () => {
      open(FORM, [approved({ publish_shared_kv: 'a/b' })]);
      await fireEvent.click(await button('Remove the publish target'));
      await save();
      expect(await screen.findByRole('dialog')).toBeInTheDocument();
    });

    it('a model or spaces around the target do not ask, since the digest does not move', async () => {
      open(FORM, [approved({ publish_shared_kv: 'a/b' })], writer);
      await edit('Publish to', ' a/b ');
      await chooseModel('smart → claude-opus');
      await save();
      expect(await sent()).toEqual({ model: 'smart', revision: 'r1' });
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });
});

describe('asks ahead of its first run', () => {
  const add = async (entry: string) => {
    await edit('Ask for', entry);
    await fireEvent.click(await button('Add'));
  };

  it('says it asks and grants nothing', async () => {
    open(FORM, [job()]);
    expect(await screen.findByText(/asks and grants nothing/)).toBeInTheDocument();
    expect(screen.getByText(/added under Network access/)).toBeInTheDocument();
    expect(screen.getByText(/does not fire until/)).toBeInTheDocument();
  });

  it('adds a row, and sends the whole list', async () => {
    open(FORM, [job({ egress: ['a.example.com'] })]);
    expect(await screen.findByText('a.example.com')).toBeInTheDocument();
    await add('skill:browse');
    expect(await screen.findByText('skill:browse')).toBeInTheDocument();
    expect(await textbox('Ask for')).toHaveValue('');
    await save();
    expect(await sent()).toEqual({
      egress: ['a.example.com', 'skill:browse'],
      revision: 'r1',
    });
  });

  it('removes a row', async () => {
    open(FORM, [job({ egress: ['a.example.com', 'b.example.com'] })]);
    await fireEvent.click(await button('Remove a.example.com'));
    expect(screen.queryByText('a.example.com')).toBeNull();
    await save();
    expect(await sent()).toEqual({ egress: ['b.example.com'], revision: 'r1' });
  });

  it('Enter in the field adds and does not submit the form', async () => {
    open(FORM, [job()]);
    await edit('Ask for', 'a.example.com');
    const notPrevented = await fireEvent.keyDown(await textbox('Ask for'), { key: 'Enter' });
    expect(notPrevented).toBe(false);
    expect(await screen.findByText('a.example.com')).toBeInTheDocument();
  });

  it('does not list an entry twice', async () => {
    open(FORM, [job({ egress: ['a.example.com'] })]);
    await add('a.example.com');
    expect(screen.getAllByText('a.example.com')).toHaveLength(1);
    expect(await button('Save')).toBeDisabled();
  });

  it('what was typed and not added goes with the save', async () => {
    open(FORM, [job()]);
    await edit('Schedule', '0 8 * * *');
    await edit('Ask for', 'a.example.com');
    await save();
    expect(await sent()).toEqual({
      cron: '0 8 * * *',
      egress: ['a.example.com'],
      revision: 'r1',
    });
  });

  it('a list left alone is not sent', async () => {
    open(FORM, [job({ egress: ['a.example.com'] })]);
    await edit('Schedule', '0 8 * * *');
    await save();
    expect(await sent()).toEqual({ cron: '0 8 * * *', revision: 'r1' });
  });

  it('a refused entry sits beside the field', async () => {
    api.updateScheduledJob.mockRejectedValueOnce(
      new JobSaveError('That is not a host.', 400, 'invalid_field', 'egress', []),
    );
    open(FORM, [job()]);
    await add('1.2.3.4');
    await save();
    expect(await screen.findByText('That is not a host.')).toBeInTheDocument();
  });

  it('a kept entry shows why it asks for nothing, and has no Remove', async () => {
    open(FORM, [job({ egress_kept: ['skill:gone'] })]);
    expect(await screen.findByText('skill:gone')).toBeInTheDocument();
    expect(screen.getByText(/asks for nothing/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove skill:gone' })).toBeNull();
  });

  it('a new job sends its entries', async () => {
    open(`${LIST}?job=new`, []);
    await edit('Name', 'morning');
    await edit('Schedule', '0 7 * * *');
    await edit('Prompt', 'Say hi.');
    await add('a.example.com');
    await save();
    await waitFor(() => expect(api.createScheduledJob).toHaveBeenCalled());
    expect(api.createScheduledJob.mock.calls[0][0].egress).toEqual(['a.example.com']);
  });
});
