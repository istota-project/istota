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
  SetupApiError,
  type SetupState,
} from '$lib/api';
import Page from './+page.svelte';

function state(phase: SetupState['phase']): SetupState {
  return {
    phase,
    local: true,
    host: 'localhost:8080',
    modules_available: [],
    memory_total_mib: 8192,
    defaults: { bot_name: 'Istota', storage: 'local', profiles: ['browser'] },
  };
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
