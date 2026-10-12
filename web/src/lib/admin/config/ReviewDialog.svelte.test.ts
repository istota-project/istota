import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { configPreview } from '$lib/test/adminConfig';
import type { ConfigChangeSet } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import ReviewDialog from './ReviewDialog.svelte';

const changeSet: ConfigChangeSet = {
  base_digest: 'digest-1',
  ops: [{ op: 'set', path: ['email', 'imap_host'], value: 'imap.example.org' }],
  raw: null,
  secrets: { istota_email_imap_password: 'pw' },
};

function mount() {
  const props = {
    changeSet,
    onClose: vi.fn(),
    onSubmitted: vi.fn(),
    onConflict: vi.fn(),
    onRequestOpen: vi.fn(),
  };
  render(ReviewDialog, { props });
  return props;
}

async function enterCode(code = '123456') {
  await screen.findByText(/a•••@example.com/);
  await fireEvent.input(screen.getByLabelText('Confirmation code'), { target: { value: code } });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
}

beforeEach(() => {
  api.startStepUp.mockResolvedValue({
    request_id: 'step-1',
    email_hint: 'a•••@example.com',
    expires_at: 'later',
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('the review', () => {
  it('previews the change set and shows the diff, credentials and the restart', async () => {
    api.previewConfigChange.mockResolvedValue(
      configPreview({ secret_names: ['istota_email_imap_password'], running_tasks: 2 }),
    );
    mount();
    expect(await screen.findByText(/\+imap_host = "new"/)).toBeInTheDocument();
    expect(api.previewConfigChange).toHaveBeenCalledWith(changeSet);
    expect(screen.getByText('istota_email_imap_password')).toBeInTheDocument();
    expect(screen.getByText(/Applying restarts Istota/)).toHaveTextContent(
      '2 tasks are running now and will be interrupted.',
    );
  });

  it('blocks on a problem and marks one already in the file as fix this first', async () => {
    api.previewConfigChange.mockResolvedValue(
      configPreview({
        problems: {
          blocking: [
            {
              kind: 'discarded',
              path: ['scheduler', 'talk_poll_timeout'],
              message: 'must be positive; keeping the default',
              blocking: true,
              pre_existing: true,
            },
          ],
          warnings: [{ kind: 'retired', path: ['old_key'], message: 'retired', blocking: false }],
        },
      }),
    );
    mount();
    expect(await screen.findByText('scheduler.talk_poll_timeout')).toBeInTheDocument();
    expect(screen.getByText('fix this first')).toBeInTheDocument();
    expect(screen.getByText('old_key')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply…' })).toBeDisabled();
  });

  it('blocks on an error and on a change that changes nothing', async () => {
    api.previewConfigChange.mockResolvedValue(
      configPreview({ errors: ['the admin list must contain you'] }),
    );
    mount();
    expect(await screen.findByText('the admin list must contain you')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply…' })).toBeDisabled();
    cleanup();

    api.previewConfigChange.mockResolvedValue(configPreview({ changed: false, diff: [] }));
    mount();
    expect(await screen.findByText('This changes nothing.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply…' })).toBeDisabled();
  });

  it('hands a changed file back to the page', async () => {
    api.previewConfigChange.mockRejectedValue(
      new api.ConfigChangeError('changed', 409, 'config_changed', { digest: 'digest-2' }),
    );
    const props = mount();
    await waitFor(() => expect(props.onConflict).toHaveBeenCalled());
  });
});

describe('applying', () => {
  it('asks for a step-up code and submits with it', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview());
    api.submitConfigChange.mockResolvedValue({ id: 17 });
    const props = mount();
    await fireEvent.click(await screen.findByRole('button', { name: 'Apply…' }));
    expect(api.startStepUp).toHaveBeenCalledWith('admin_config_change', undefined);
    await enterCode();
    await waitFor(() => expect(props.onSubmitted).toHaveBeenCalledWith(17));
    expect(api.submitConfigChange).toHaveBeenCalledWith({
      ...changeSet,
      confirm: [],
      step_up: { request_id: 'step-1', code: '123456' },
    });
  });

  it('requires the typed phrase before a risky change reaches step-up', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview({ risky_keys: ['web.auth'] }));
    api.submitConfigChange.mockResolvedValue({ id: 3 });
    const props = mount();
    await fireEvent.click(await screen.findByRole('button', { name: 'Apply…' }));
    const proceed = await screen.findByRole('button', { name: 'Continue' });
    expect(proceed).toBeDisabled();
    expect(api.startStepUp).not.toHaveBeenCalled();
    await fireEvent.input(screen.getByLabelText('Type to confirm'), { target: { value: 'apply' } });
    expect(proceed).toBeEnabled();
    await fireEvent.click(proceed);
    await enterCode();
    await waitFor(() => expect(props.onSubmitted).toHaveBeenCalledWith(3));
    expect(api.submitConfigChange.mock.calls[0][0].confirm).toEqual(['web.auth']);
  });

  it('keeps a refused code in the code dialog', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview());
    api.submitConfigChange.mockRejectedValue(
      new api.ConfigChangeError('That code was not accepted. Try again.', 403, null, {
        field: 'code',
        reason: 'bad',
      }),
    );
    const props = mount();
    await fireEvent.click(await screen.findByRole('button', { name: 'Apply…' }));
    await enterCode();
    expect(await screen.findByText('That code was not accepted. Try again.')).toBeInTheDocument();
    expect(screen.getByLabelText('Confirmation code')).toBeInTheDocument();
    expect(props.onSubmitted).not.toHaveBeenCalled();
  });

  it('returns to the review with any other refusal, and follows an open request', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview());
    api.submitConfigChange.mockRejectedValueOnce(
      new api.ConfigChangeError('A plan manages part of this change.', 409, 'managed', {
        resources: ['secrets'],
      }),
    );
    const props = mount();
    await fireEvent.click(await screen.findByRole('button', { name: 'Apply…' }));
    await enterCode();
    expect(await screen.findByText('A plan manages part of this change.')).toBeInTheDocument();
    expect(screen.getByText('Managed: secrets')).toBeInTheDocument();

    api.submitConfigChange.mockRejectedValueOnce(
      new api.ConfigChangeError('waiting', 409, 'request_open', { id: 9 }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Apply…' }));
    await enterCode();
    await waitFor(() => expect(props.onRequestOpen).toHaveBeenCalledWith(9));
  });
});

describe('a preview already in hand', () => {
  it('shows it and its note without asking again, and asks on Check again', async () => {
    const restored = configPreview({
      problems: {
        blocking: [],
        warnings: [
          {
            kind: 'warning',
            path: ['email', 'imap_password'],
            message: 'email.imap_password was cleared after this change; it stays cleared',
            blocking: false,
          },
        ],
      },
    });
    render(ReviewDialog, {
      props: {
        changeSet: { ...changeSet, ops: [], raw: 'bot_name = "x"\n', secrets: {} },
        initialPreview: restored,
        note: 'Restoring change #12.',
        onClose: vi.fn(),
        onSubmitted: vi.fn(),
        onConflict: vi.fn(),
        onRequestOpen: vi.fn(),
      },
    });
    expect(await screen.findByText(/was cleared after this change/)).toBeInTheDocument();
    expect(screen.getByText('Restoring change #12.')).toBeInTheDocument();
    expect(api.previewConfigChange).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Apply…' })).toBeEnabled();

    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenCalledWith(
        expect.objectContaining({ ops: [], raw: 'bot_name = "x"\n' }),
      ),
    );
  });
});
