/**
 * The setup page's frame: unlock, the configured phase, and `?step=`.
 *
 * Named without the `+` for the reason `layout.svelte.test.ts` gives: Kit
 * reserves the prefix. The step screens themselves are later stages; here a
 * step is its card title, which is enough to tell which one is on screen.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { __history } from '../../../vitest-stubs/app-navigation';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import {
  getSetupState,
  unlockSetup,
  getSetupDraft,
  putSetupDraft,
  validateSetupStep,
  checkSetupConnection,
  getSetupModels,
  SetupApiError,
  type SetupDraft,
  type SetupDraftPatch,
  type SetupState,
} from '$lib/api';
import Page from './+page.svelte';

const MODELS = {
  anthropic: { base_url: 'https://api.anthropic.com/v1', model: 'claude-sonnet-4-6' },
  openrouter: { base_url: 'https://openrouter.ai/api/v1', model: 'anthropic/claude-sonnet-4.6' },
};
const CREDENTIALS = new Set(['password', 'native_api_key', 'claude_code_oauth_token']);

function state(phase: SetupState['phase']): SetupState {
  return {
    phase,
    local: true,
    host: 'localhost:8080',
    modules_available: [],
    memory_total_mib: 8192,
    defaults: { bot_name: 'Istota', storage: 'local', profiles: ['browser'], models: MODELS },
  };
}

/** The server's draft, as `PUT /draft` keeps it: merged, credentials redacted. */
let saved: Record<string, unknown> = {};
function redacted(): SetupDraft {
  const out: SetupDraft = {};
  for (const [key, value] of Object.entries(saved)) {
    out[key] = CREDENTIALS.has(key) ? { set: Boolean(value) } : (value as SetupDraft[string]);
  }
  return out;
}
function serveDraft() {
  saved = {};
  vi.mocked(putSetupDraft).mockImplementation(async (patch: SetupDraftPatch) => {
    for (const [key, value] of Object.entries(patch)) {
      if (value === null) {
        if (!CREDENTIALS.has(key)) delete saved[key];
      } else saved[key] = value;
    }
    return { draft: redacted() };
  });
}

const currentUrl = () => __history.entries[__history.index].url;
const refused = () =>
  new SetupApiError(
    403,
    'forbidden',
    'That setup code is not valid, or it has already been used. Run `sudo istota-stack setup --web` again for a new one.',
  );

beforeEach(() => {
  vi.clearAllMocks();
  __history.reset('/setup/');
  vi.mocked(getSetupState).mockResolvedValue(state('locked'));
  vi.mocked(unlockSetup).mockResolvedValue(undefined);
  vi.mocked(getSetupDraft).mockResolvedValue({ draft: {} });
  vi.mocked(validateSetupStep).mockResolvedValue({ ok: true, fields: [] });
  vi.mocked(getSetupModels).mockResolvedValue([]);
  serveDraft();
});

afterEach(cleanup);

describe('unlocking with the code from the address', () => {
  it('posts the fragment code once, clears it, and opens the first screen', async () => {
    __history.reset('/setup/#code=7KQ2M-X9D4F-H3T8W-QP6ZA');

    render(Page);

    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(unlockSetup).toHaveBeenCalledTimes(1);
    expect(unlockSetup).toHaveBeenCalledWith('7KQ2M-X9D4F-H3T8W-QP6ZA');
    expect(window.location.hash).toBe('');
    expect(currentUrl()).not.toContain('code=');
  });

  it('shows the unlock screen with the refusal when the fragment code is spent', async () => {
    __history.reset('/setup/#code=7KQ2M-X9D4F-H3T8W-QP6ZA');
    vi.mocked(unlockSetup).mockRejectedValue(refused());

    render(Page);

    expect(await screen.findByText(/already been used/)).toBeInTheDocument();
    // A regex: `Field` puts its error inside the label, so the name grows.
    expect(screen.getByLabelText(/^Setup code/)).toHaveValue('');
    expect(unlockSetup).toHaveBeenCalledTimes(1);
    expect(window.location.hash).toBe('');
  });

  it('clears a fragment code without posting it when the session is already open', async () => {
    __history.reset('/setup/#code=7KQ2M-X9D4F-H3T8W-QP6ZA');
    vi.mocked(getSetupState).mockResolvedValue(state('open'));

    render(Page);

    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(unlockSetup).not.toHaveBeenCalled();
    expect(window.location.hash).toBe('');
  });
});

describe('the unlock screen', () => {
  it('asks for the code when the address carried none', async () => {
    render(Page);

    const input = await screen.findByLabelText('Setup code');
    expect(input).toHaveAttribute('autocomplete', 'one-time-code');
    expect(screen.getByText('Where do I find this?')).toBeInTheDocument();
    expect(unlockSetup).not.toHaveBeenCalled();
  });

  it('unlocks with a typed code and opens the first screen', async () => {
    render(Page);

    const input = await screen.findByLabelText('Setup code');
    await fireEvent.input(input, { target: { value: '7kq2m x9d4f' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(unlockSetup).toHaveBeenCalledWith('7kq2m x9d4f');
    expect(getSetupDraft).toHaveBeenCalled();
  });

  it('shows a refused code under the field and stays locked', async () => {
    vi.mocked(unlockSetup).mockRejectedValue(refused());
    render(Page);

    const input = await screen.findByLabelText('Setup code');
    await fireEvent.input(input, { target: { value: 'WRONG' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText(/already been used/)).toBeInTheDocument();
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(screen.queryByRole('heading', { name: 'Welcome' })).toBeNull();
    expect(getSetupDraft).not.toHaveBeenCalled();
  });

  it('refuses an empty code without asking the server', async () => {
    render(Page);

    await screen.findByLabelText('Setup code');
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText('Enter your setup code.')).toBeInTheDocument();
    expect(unlockSetup).not.toHaveBeenCalled();
  });

  it('goes back to the unlock screen when the session is gone', async () => {
    vi.mocked(getSetupState).mockResolvedValue(state('open'));
    vi.mocked(getSetupDraft).mockRejectedValue(
      new SetupApiError(401, 'no_session', 'Setup is locked. Enter your setup code to continue.'),
    );

    render(Page);

    expect(await screen.findByLabelText('Setup code')).toBeInTheDocument();
  });
});

describe('a machine that is already set up', () => {
  it('says so and offers the sign-in page, asking nothing else', async () => {
    __history.reset('/setup/#code=7KQ2M-X9D4F-H3T8W-QP6ZA');
    vi.mocked(getSetupState).mockResolvedValue({ phase: 'configured' });

    render(Page);

    expect(await screen.findByText('Istota is already set up')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login');
    expect(unlockSetup).not.toHaveBeenCalled();
    expect(getSetupDraft).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('Setup code')).toBeNull();
  });
});

describe('an unreachable setup service', () => {
  it('says so and retries on request', async () => {
    vi.mocked(getSetupState).mockRejectedValueOnce(
      new SetupApiError(0, 'unreachable', "Can't reach the setup service."),
    );

    render(Page);

    expect(await screen.findByText("Can't reach the setup service")).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByLabelText('Setup code')).toBeInTheDocument();
    expect(getSetupState).toHaveBeenCalledTimes(2);
  });
});

describe('the step in the address', () => {
  beforeEach(() => {
    vi.mocked(getSetupState).mockResolvedValue(state('open'));
  });

  it('opens the step a reload names', async () => {
    __history.reset('/setup/?step=model');

    render(Page);

    expect(await screen.findByRole('heading', { name: 'AI service' })).toBeInTheDocument();
    expect(screen.getAllByText(/Step 3 of 8/).length).toBeGreaterThan(0);
  });

  it('falls back to the first step for a step it does not know', async () => {
    __history.reset('/setup/?step=nonsense');

    render(Page);

    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    await waitFor(() => expect(currentUrl()).toBe('/setup/?step=welcome'));
    expect(__history.entries).toHaveLength(1);
  });

  it('pushes a history entry per Next, so the browser’s Back returns', async () => {
    render(Page);
    await screen.findByRole('heading', { name: 'Welcome' });

    await fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByRole('heading', { name: 'Your account' })).toBeInTheDocument();
    expect(currentUrl()).toBe('/setup/?step=account');

    __history.back();
    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
  });

  it('moves back a step with the Back button and has none on the first', async () => {
    __history.reset('/setup/?step=account');
    render(Page);
    await screen.findByRole('heading', { name: 'Your account' });

    await fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Back' })).toBeNull();
  });

  it('names the bot from the draft in the header', async () => {
    vi.mocked(getSetupDraft).mockResolvedValue({ draft: { bot_name: 'Marlowe' } });

    render(Page);

    expect(await screen.findByText('Marlowe')).toBeInTheDocument();
  });
});

async function openAt(step: string) {
  vi.mocked(getSetupState).mockResolvedValue(state('open'));
  __history.reset(`/setup/?step=${step}`);
  render(Page);
}

const next = () => fireEvent.click(screen.getByRole('button', { name: 'Next' }));
const type = (label: RegExp, value: string) =>
  fireEvent.input(screen.getByLabelText(label), { target: { value } });

describe('the Welcome screen', () => {
  it('starts from the bot name and the browser’s time zone', async () => {
    await openAt('welcome');
    expect(await screen.findByLabelText(/^What should your assistant be called/)).toHaveValue(
      'Istota',
    );
    expect(screen.getByLabelText(/^Time zone/)).toHaveValue(
      Intl.DateTimeFormat().resolvedOptions().timeZone,
    );
  });

  it('suggests a username from the email until one is typed', async () => {
    await openAt('welcome');
    await screen.findByLabelText(/^Your email/);
    await type(/^Your email/, 'Ada.Lovelace@example.com');
    expect(screen.getByLabelText(/^Username/)).toHaveValue('ada.lovelace');

    await type(/^Username/, 'countess');
    await type(/^Your email/, 'ada@example.com');
    expect(screen.getByLabelText(/^Username/)).toHaveValue('countess');
  });

  it('saves the answers, has the server check them, and moves on', async () => {
    await openAt('welcome');
    await screen.findByLabelText(/^Your name/);
    await type(/^Your name/, 'Ada Lovelace');
    await type(/^Your email/, 'ada@example.com');
    await next();

    expect(await screen.findByRole('heading', { name: 'Your account' })).toBeInTheDocument();
    expect(saved).toMatchObject({
      bot_name: 'Istota',
      display_name: 'Ada Lovelace',
      user_email: 'ada@example.com',
      user_id: 'ada',
    });
    expect(validateSetupStep).toHaveBeenCalledWith('welcome');
    expect(vi.mocked(putSetupDraft).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(validateSetupStep).mock.invocationCallOrder[0],
    );
  });

  it('shows the server’s refusals under their fields and stays', async () => {
    vi.mocked(validateSetupStep).mockResolvedValue({
      ok: false,
      fields: [
        { field: 'user_email', message: "That doesn't look like an email address." },
        { field: 'user_id', message: 'A username is up to 32 lowercase letters.' },
      ],
    });
    await openAt('welcome');
    await screen.findByLabelText(/^Your email/);
    await type(/^Your email/, 'not-an-address');
    await next();

    expect(await screen.findByText("That doesn't look like an email address.")).toBeInTheDocument();
    // The username's error opens the fold it sits in.
    expect(screen.getByText('A username is up to 32 lowercase letters.')).toBeVisible();
    expect(screen.getByLabelText(/^Your email/)).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(currentUrl()).toBe('/setup/?step=welcome');
  });

  it('shows an error about the form as a whole in a banner', async () => {
    vi.mocked(putSetupDraft).mockRejectedValue(
      new SetupApiError(0, 'unreachable', "Can't reach the setup service."),
    );
    await openAt('welcome');
    await screen.findByLabelText(/^Your email/);
    await next();

    expect(await screen.findByText("Can't reach the setup service.")).toBeInTheDocument();
    expect(validateSetupStep).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
  });

  it('goes back to the unlock screen when the session is gone', async () => {
    vi.mocked(validateSetupStep).mockRejectedValue(
      new SetupApiError(401, 'no_session', 'Setup is locked. Enter your setup code to continue.'),
    );
    await openAt('welcome');
    await screen.findByLabelText(/^Your email/);
    await next();
    expect(await screen.findByLabelText('Setup code')).toBeInTheDocument();
  });
});

describe('Back', () => {
  it('keeps what was typed on a screen, saved or not', async () => {
    await openAt('welcome');
    await screen.findByLabelText(/^Your name/);
    await type(/^Your name/, 'Ada Lovelace');
    await type(/^Your email/, 'ada@example.com');
    await next();
    await screen.findByRole('heading', { name: 'Your account' });

    await type(/^Password/, 'a long enough passphrase');
    await fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    await screen.findByRole('heading', { name: 'Welcome' });
    expect(screen.getByLabelText(/^Your name/)).toHaveValue('Ada Lovelace');
    expect(screen.getByLabelText(/^Your email/)).toHaveValue('ada@example.com');

    await next();
    await screen.findByRole('heading', { name: 'Your account' });
    expect(screen.getByLabelText(/^Password/)).toHaveValue('a long enough passphrase');
    // An unconfirmed password is not saved by another screen's Next.
    expect(saved).not.toHaveProperty('password');
  });
});

describe('the Your account screen', () => {
  it('refuses two different passwords without saving either', async () => {
    await openAt('account');
    await screen.findByLabelText(/^Password/);
    await type(/^Password/, 'a long enough passphrase');
    await type(/^Confirm password/, 'a different passphrase');
    await next();

    expect(await screen.findByText("The two passwords don't match.")).toBeInTheDocument();
    expect(putSetupDraft).not.toHaveBeenCalled();
    expect(validateSetupStep).not.toHaveBeenCalled();
  });

  it('saves a confirmed password and shows the server’s rule when it is refused', async () => {
    vi.mocked(validateSetupStep).mockResolvedValueOnce({
      ok: false,
      fields: [{ field: 'password', message: 'Password must be at least 12 characters' }],
    });
    await openAt('account');
    await screen.findByLabelText(/^Password/);
    await type(/^Password/, 'too short');
    await type(/^Confirm password/, 'too short');
    await next();

    expect(await screen.findByText('Password must be at least 12 characters')).toBeInTheDocument();
    expect(saved.password).toBe('too short');
    expect(validateSetupStep).toHaveBeenCalledWith('account');
    expect(screen.getByRole('heading', { name: 'Your account' })).toBeInTheDocument();
  });

  it('moves on with a password already saved and nothing typed', async () => {
    vi.mocked(getSetupDraft).mockResolvedValue({ draft: { password: { set: true } } });
    await openAt('account');
    await screen.findByLabelText(/^Password/);
    await next();
    expect(await screen.findByRole('heading', { name: 'AI service' })).toBeInTheDocument();
  });
});

describe('the AI service screen', () => {
  const passed = { ok: true, warning: false, message: 'Connected, and the model answered.' };
  const refused = { ok: false, warning: false, message: 'That API key was not accepted.' };

  it('offers the three services in order, the Anthropic key first and checked', async () => {
    await openAt('model');
    const cards = await screen.findAllByRole('radio');
    expect(cards.map((c) => c.textContent)).toEqual([
      expect.stringContaining('Anthropic API key'),
      expect.stringContaining('OpenRouter API key'),
      expect.stringContaining('Claude subscription'),
    ]);
    expect(cards[0]).toHaveAttribute('aria-checked', 'true');
    expect(cards[0].textContent).toContain('Recommended');
  });

  it('tests the key on Next and moves on when it connects', async () => {
    vi.mocked(checkSetupConnection).mockResolvedValue(passed);
    await openAt('model');
    await screen.findByLabelText(/^Anthropic API key/);
    await type(/^Anthropic API key/, 'test-key-1');
    await next();

    expect(await screen.findByRole('heading', { name: 'Web address' })).toBeInTheDocument();
    expect(saved).toMatchObject({
      brain_kind: 'native',
      native_base_url: MODELS.anthropic.base_url,
      native_model: MODELS.anthropic.model,
      native_api_key: 'test-key-1',
    });
    expect(checkSetupConnection).toHaveBeenCalledWith('model');
    // The draft is saved before the test, which reads it.
    expect(vi.mocked(putSetupDraft).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(checkSetupConnection).mock.invocationCallOrder[0],
    );
  });

  it('stays on a failed test and offers to continue without it', async () => {
    vi.mocked(checkSetupConnection).mockResolvedValue(refused);
    await openAt('model');
    await screen.findByLabelText(/^Anthropic API key/);
    await type(/^Anthropic API key/, 'wrong-key');
    await next();

    expect(await screen.findByText('That API key was not accepted.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'AI service' })).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'Continue without testing' }));
    expect(await screen.findByRole('heading', { name: 'Web address' })).toBeInTheDocument();
    expect(checkSetupConnection).toHaveBeenCalledTimes(1);
    expect(validateSetupStep).toHaveBeenCalledTimes(2);
  });

  it('does not test again on Next after the Test button passed', async () => {
    vi.mocked(checkSetupConnection).mockResolvedValue(passed);
    await openAt('model');
    await screen.findByLabelText(/^Anthropic API key/);
    await type(/^Anthropic API key/, 'test-key-1');
    await fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    expect(await screen.findByText(passed.message)).toBeInTheDocument();

    await next();
    expect(await screen.findByRole('heading', { name: 'Web address' })).toBeInTheDocument();
    expect(checkSetupConnection).toHaveBeenCalledTimes(1);
  });

  it('tests again once the key changes after a pass', async () => {
    vi.mocked(checkSetupConnection).mockResolvedValueOnce(passed).mockResolvedValueOnce(refused);
    await openAt('model');
    await screen.findByLabelText(/^Anthropic API key/);
    await type(/^Anthropic API key/, 'test-key-1');
    await fireEvent.click(screen.getByRole('button', { name: 'Test' }));
    await screen.findByText(passed.message);

    await type(/^Anthropic API key/, 'test-key-2');
    expect(screen.queryByText(passed.message)).toBeNull();
    await next();
    expect(await screen.findByText(refused.message)).toBeInTheDocument();
    expect(checkSetupConnection).toHaveBeenCalledTimes(2);
  });

  it('shows a test the server could not run under the fields it names', async () => {
    vi.mocked(checkSetupConnection).mockRejectedValue(
      new SetupApiError(400, 'incomplete', 'Fill in the fields above to test the connection.', [
        { field: 'native_api_key', message: 'Enter an API key to test.' },
      ]),
    );
    await openAt('model');
    await screen.findByLabelText(/^Anthropic API key/);
    await fireEvent.click(screen.getByRole('button', { name: 'Test' }));

    expect(await screen.findByText('Enter an API key to test.')).toBeInTheDocument();
    expect(
      screen.getByText('Fill in the fields above to test the connection.'),
    ).toBeInTheDocument();
  });

  it('switches to OpenRouter’s address and model, with its models as suggestions', async () => {
    vi.mocked(getSetupModels).mockResolvedValue(['anthropic/claude-sonnet-4.6', 'z-ai/glm-5']);
    vi.mocked(checkSetupConnection).mockResolvedValue(passed);
    await openAt('model');
    await fireEvent.click(await screen.findByRole('radio', { name: /OpenRouter API key/ }));

    expect(await screen.findByLabelText(/^OpenRouter API key/)).toBeInTheDocument();
    expect(getSetupModels).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/^Model/)).toHaveValue(MODELS.openrouter.model);

    await type(/^OpenRouter API key/, 'or-key');
    await type(/^Model/, 'z-ai/glm-5');
    await next();
    await screen.findByRole('heading', { name: 'Web address' });
    expect(saved).toMatchObject({
      brain_kind: 'native',
      native_base_url: MODELS.openrouter.base_url,
      native_model: 'z-ai/glm-5',
      native_api_key: 'or-key',
    });
  });

  it('drops one service’s stored key on switching to the other', async () => {
    vi.mocked(getSetupDraft).mockResolvedValue({
      draft: {
        brain_kind: 'native',
        native_base_url: MODELS.anthropic.base_url,
        native_model: MODELS.anthropic.model,
        native_api_key: { set: true },
      },
    });
    vi.mocked(checkSetupConnection).mockResolvedValue(passed);
    await openAt('model');
    await fireEvent.click(await screen.findByRole('radio', { name: /OpenRouter API key/ }));
    await type(/^OpenRouter API key/, 'or-key');
    await next();
    await screen.findByRole('heading', { name: 'Web address' });
    expect(vi.mocked(putSetupDraft).mock.calls[0][0]).toMatchObject({ native_api_key: 'or-key' });

    // Without a new key, the old service's is erased rather than kept.
    vi.mocked(putSetupDraft).mockClear();
    __history.back();
    await screen.findByRole('heading', { name: 'AI service' });
    await fireEvent.click(screen.getByRole('radio', { name: /Anthropic API key/ }));
    await next();
    expect(vi.mocked(putSetupDraft).mock.calls[0][0]).toMatchObject({ native_api_key: '' });
  });

  it('asks a subscription for its token and does not test it', async () => {
    await openAt('model');
    await fireEvent.click(await screen.findByRole('radio', { name: /Claude subscription/ }));
    expect(screen.getByText(/claude setup-token/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Test' })).toBeNull();

    await type(/^Claude Code token/, 'subscription-token');
    await next();
    expect(await screen.findByRole('heading', { name: 'Web address' })).toBeInTheDocument();
    expect(saved).toMatchObject({
      brain_kind: 'claude_code',
      claude_code_oauth_token: 'subscription-token',
    });
    expect(checkSetupConnection).not.toHaveBeenCalled();
  });

  it('keeps a typed model under More when the server refuses it', async () => {
    vi.mocked(validateSetupStep).mockResolvedValue({
      ok: false,
      fields: [{ field: 'native_model', message: 'Enter a model name.' }],
    });
    await openAt('model');
    await screen.findByLabelText(/^Anthropic API key/);
    await type(/^Model/, '');
    await next();
    expect(await screen.findByText('Enter a model name.')).toBeVisible();
    expect(checkSetupConnection).not.toHaveBeenCalled();
  });
});
