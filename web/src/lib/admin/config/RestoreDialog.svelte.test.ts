import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { configPreview } from '$lib/test/adminConfig';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import {
  ConfigChangeError,
  type ConfigRestoreCredential,
  type ConfigRestorePreview,
} from '$lib/api';
import RestoreDialog from './RestoreDialog.svelte';

const KDBX = new Uint8Array([0x03, 0xd9, 0xa2, 0x9a, 0x67, 0xfb, 0x4b, 0xb5, 1, 2, 3]);
const kdbxBase64 = btoa(String.fromCharCode(...KDBX));
const plainText = '[web]\nport = 8080\n';
const backupPassword = 'fixture-restore-password';

function credential(overrides: Partial<ConfigRestoreCredential>): ConfigRestoreCredential {
  return {
    id: 'file:istota_x',
    kind: 'file',
    name: 'istota_x',
    source: 'file',
    status: 'new',
    reason: null,
    selectable: true,
    ...overrides,
  };
}

const credentials: ConfigRestoreCredential[] = [
  credential({ id: 'file:istota_email_password', name: 'Email password', status: 'changed' }),
  credential({ id: 'file:istota_brave_api_key', name: 'Brave API key', status: 'new' }),
  credential({
    id: 'file:istota_model_api_key',
    name: 'Model API key',
    status: 'unchanged',
    selectable: false,
  }),
  credential({
    id: 'file:istota_nextcloud_app_password',
    name: 'Nextcloud app password',
    status: 'host_only',
    reason: 'Changed with the mount on the host.',
    selectable: false,
  }),
  credential({
    id: 'inline:["web","token"]',
    kind: 'inline',
    name: 'web.token',
    source: 'inline',
    status: 'new',
  }),
];

function restorePreview(overrides: Partial<ConfigRestorePreview> = {}): ConfigRestorePreview {
  return {
    preview: configPreview({ secret_names: [] }),
    input_digest: 'input-1',
    preview_digest: 'preview-1',
    credentials: [],
    note: 'Restoring the configuration replaces the whole file.',
    ...overrides,
  };
}

function mount(props: Record<string, unknown> = {}) {
  const all = {
    baseDigest: 'digest-1',
    onClose: vi.fn(),
    onSubmitted: vi.fn(),
    onConflict: vi.fn(),
    onRequestOpen: vi.fn(),
    ...props,
  };
  const component = render(RestoreDialog, { props: all });
  return { ...all, component };
}

async function pick(bytes: BlobPart, name: string) {
  const file = new File([bytes], name);
  await fireEvent.change(screen.getByLabelText('Backup file', { selector: 'input' }), {
    target: { files: [file] },
  });
}

async function enterCode() {
  await screen.findByText(/a•••@example.com/);
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
}

async function unlock() {
  await pick(KDBX, 'backup.kdbx');
  await fireEvent.input(await screen.findByLabelText('Backup password'), {
    target: { value: backupPassword },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Unlock' }));
}

function box(name: string) {
  return screen.getByLabelText(name, { exact: false, selector: 'input' });
}

beforeEach(() => {
  api.startStepUp.mockResolvedValue({
    request_id: 'step-1',
    method: 'email' as const,
    email_hint: 'a•••@example.com',
    expires_at: 'later',
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('a plain backup', () => {
  it('previews the file as a configuration-only restore and submits it', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview());
    api.submitConfigRestore.mockResolvedValue({ id: 31 });
    const props = mount();
    await pick(plainText, 'istota-config.toml');
    expect(screen.queryByLabelText('Backup password')).toBeNull();
    await fireEvent.click(await screen.findByRole('button', { name: 'Preview' }));

    expect(await screen.findByText(/\+imap_host = "new"/)).toBeInTheDocument();
    expect(screen.getByText(/replaces the whole file/)).toBeInTheDocument();
    const upload = {
      format: 'toml',
      file: btoa(plainText),
      password: null,
      keyfile: null,
      base_digest: 'digest-1',
      restore_config: true,
      selected: [],
    };
    expect(api.previewConfigRestore).toHaveBeenCalledWith(upload, expect.any(AbortSignal));
    expect(api.previewConfigChange).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button', { name: 'Apply…' }));
    expect(api.startStepUp).toHaveBeenCalledWith('admin_config_change', undefined);
    await enterCode();
    await waitFor(() => expect(props.onSubmitted).toHaveBeenCalledWith(31));
    expect(api.submitConfigRestore).toHaveBeenCalledWith(
      {
        ...upload,
        input_digest: 'input-1',
        preview_digest: 'preview-1',
        confirm: [],
        step_up: { request_id: 'step-1', code: '123456' },
      },
      expect.any(AbortSignal),
    );
    expect(api.submitConfigChange).not.toHaveBeenCalled();
  });

  it('forgets a file whose picker was cancelled', async () => {
    mount();
    await pick(plainText, 'istota-config.toml');
    expect(await screen.findByRole('button', { name: 'Preview' })).toBeEnabled();
    await fireEvent.change(screen.getByLabelText('Backup file', { selector: 'input' }), {
      target: { files: [] },
    });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Preview' })).toBeDisabled());
  });
});

describe('an encrypted backup', () => {
  it('lists credentials without values, restores none by default, and selects on request', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview({ credentials }));
    mount();
    await pick(KDBX, 'backup.kdbx');
    expect(screen.getByRole('button', { name: 'Unlock' })).toBeDisabled();
    await fireEvent.input(screen.getByLabelText('Backup password'), {
      target: { value: backupPassword },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Unlock' }));

    expect(await screen.findByText('Email password')).toBeInTheDocument();
    expect(api.previewConfigRestore).toHaveBeenCalledWith(
      expect.objectContaining({
        format: 'kdbx',
        file: kdbxBase64,
        password: backupPassword,
        keyfile: null,
        restore_config: true,
        selected: [],
      }),
      expect.any(AbortSignal),
    );
    expect(box('Restore the configuration')).toBeChecked();
    expect(box('Email password')).not.toBeChecked();
    expect(box('Email password')).toBeEnabled();
    expect(box('Model API key')).toBeDisabled();
    expect(box('Nextcloud app password')).toBeDisabled();
    expect(screen.getByText('Changed with the mount on the host.')).toBeInTheDocument();
    expect(screen.getByText(/Replaces the value this installation has/)).toBeInTheDocument();
    // The unlock material is fixed once the file is open.
    expect(screen.getByLabelText('Backup password')).toBeDisabled();

    await fireEvent.click(screen.getByRole('button', { name: 'Select all that can be restored' }));
    expect(box('Email password')).toBeChecked();
    expect(box('Brave API key')).toBeChecked();
    expect(box('web.token')).toBeChecked();
    expect(box('Model API key')).not.toBeChecked();
  });

  it('previews the chosen selection again, names what it replaces, and submits it', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview({ credentials }));
    api.submitConfigRestore.mockResolvedValue({ id: 32 });
    const props = mount();
    await unlock();
    await fireEvent.click(
      await screen.findByLabelText('Email password', { exact: false, selector: 'input' }),
    );
    await fireEvent.click(box('Restore the configuration'));
    api.previewConfigRestore.mockResolvedValue(
      restorePreview({
        credentials,
        input_digest: 'input-2',
        preview_digest: 'preview-2',
        preview: configPreview({ diff: [], secret_names: ['istota_email_password'] }),
      }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review restore…' }));

    expect(await screen.findByText('istota_email_password')).toBeInTheDocument();
    expect(screen.getByText(/Replaces the current value of Email password/)).toBeInTheDocument();
    expect(api.previewConfigRestore).toHaveBeenCalledTimes(2);
    expect(api.previewConfigRestore.mock.calls[1][0]).toMatchObject({
      restore_config: false,
      selected: ['file:istota_email_password'],
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Apply…' }));
    await enterCode();
    await waitFor(() => expect(props.onSubmitted).toHaveBeenCalledWith(32));
    expect(api.submitConfigRestore.mock.calls[0][0]).toMatchObject({
      format: 'kdbx',
      password: backupPassword,
      restore_config: false,
      selected: ['file:istota_email_password'],
      input_digest: 'input-2',
      preview_digest: 'preview-2',
    });
  });

  it('reuses the unlock preview when the selection has not changed', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview({ credentials }));
    mount();
    await unlock();
    await fireEvent.click(await screen.findByRole('button', { name: 'Review restore…' }));
    expect(await screen.findByRole('button', { name: 'Apply…' })).toBeEnabled();
    expect(api.previewConfigRestore).toHaveBeenCalledTimes(1);
  });

  it('sends a key file as base64', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview({ credentials }));
    mount();
    await pick(KDBX, 'backup.kdbx');
    const key = '<KeyFile><Meta><Version>2.0</Version></Meta></KeyFile>';
    await fireEvent.change(screen.getByLabelText('Key file (optional)', { selector: 'input' }), {
      target: { files: [new File([key], 'backup.keyx')] },
    });
    await fireEvent.click(await screen.findByRole('button', { name: 'Unlock' }));
    await screen.findByText('Email password');
    expect(api.previewConfigRestore.mock.calls[0][0]).toMatchObject({
      password: null,
      keyfile: btoa(key),
    });
  });

  it('keeps a wrong password editable and shows the refusal', async () => {
    api.previewConfigRestore.mockRejectedValueOnce(
      new ConfigChangeError('Wrong password or key file.', 400, 'backup_locked'),
    );
    mount();
    await unlock();
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong password or key file.');
    expect(screen.getByLabelText('Backup password')).toBeEnabled();
    expect(screen.queryByText('Email password')).toBeNull();
  });

  it('keeps a busy refusal retryable without sending again', async () => {
    api.previewConfigRestore.mockResolvedValueOnce(restorePreview({ credentials }));
    mount();
    await unlock();
    await fireEvent.click(
      await screen.findByLabelText('Brave API key', { exact: false, selector: 'input' }),
    );
    api.previewConfigRestore.mockRejectedValueOnce(
      new ConfigChangeError('Another backup is being opened.', 503, 'backup_busy'),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review restore…' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Another backup is being opened.');
    expect(api.previewConfigRestore).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('button', { name: 'Apply…' })).toBeNull();
    expect(box('Brave API key')).toBeChecked();
    expect(screen.getByRole('button', { name: 'Review restore…' })).toBeEnabled();
  });
});

describe('a stale review', () => {
  it('reloads on a changed config.toml, keeps the file, and asks for a fresh preview', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview({ credentials }));
    api.submitConfigRestore.mockRejectedValueOnce(
      new ConfigChangeError('config.toml changed.', 409, 'config_changed'),
    );
    const props = mount();
    await unlock();
    await fireEvent.click(await screen.findByRole('button', { name: 'Review restore…' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Apply…' }));
    await enterCode();
    await waitFor(() => expect(props.onConflict).toHaveBeenCalled());
    expect(await screen.findByText(/config.toml changed on the host/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply…' })).toBeNull();
    expect(screen.getByLabelText('Backup password')).toHaveValue(backupPassword);

    await fireEvent.click(screen.getByRole('button', { name: 'Unlock' }));
    await waitFor(() => expect(api.previewConfigRestore).toHaveBeenCalledTimes(2));
    expect(api.submitConfigRestore).toHaveBeenCalledTimes(1);
  });

  it('drops the preview when the server rebuilt a different restore', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview({ credentials }));
    api.submitConfigRestore.mockRejectedValueOnce(
      new ConfigChangeError(
        'The restore differs from the one reviewed. Preview it again.',
        409,
        'preview_changed',
      ),
    );
    mount();
    await unlock();
    await fireEvent.click(await screen.findByRole('button', { name: 'Review restore…' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Apply…' }));
    await enterCode();
    expect(await screen.findByRole('alert')).toHaveTextContent(/differs from the one reviewed/);
    expect(screen.queryByRole('button', { name: 'Apply…' })).toBeNull();
    expect(screen.queryByText('Email password')).toBeNull();
    expect(screen.getByRole('button', { name: 'Unlock' })).toBeEnabled();
  });

  it('follows a request that is already open', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview());
    api.submitConfigRestore.mockRejectedValueOnce(
      new ConfigChangeError('waiting', 409, 'request_open', { id: 9 }),
    );
    const props = mount();
    await pick(plainText, 'istota-config.toml');
    await fireEvent.click(await screen.findByRole('button', { name: 'Preview' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Apply…' }));
    await enterCode();
    await waitFor(() => expect(props.onRequestOpen).toHaveBeenCalledWith(9));
  });
});

describe('the dialog', () => {
  it('inspects a backup on a read-only installation without offering Apply', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview());
    mount({ applyBlockedReason: 'No host applier.' });
    await pick(plainText, 'istota-config.toml');
    await fireEvent.click(await screen.findByRole('button', { name: 'Preview' }));
    expect(await screen.findByText('No host applier.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply…' })).toBeDisabled();
  });

  it('goes back to the file from the review, and Cancel closes', async () => {
    api.previewConfigRestore.mockResolvedValue(restorePreview());
    const props = mount();
    await pick(plainText, 'istota-config.toml');
    await fireEvent.click(await screen.findByRole('button', { name: 'Preview' }));
    await screen.findByRole('button', { name: 'Apply…' });
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(await screen.findByLabelText('Backup file', { selector: 'input' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply…' })).toBeNull();
    expect(props.onClose).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(props.onClose).toHaveBeenCalled();
    expect(api.submitConfigRestore).not.toHaveBeenCalled();
  });

  it('aborts a preview on unmount and ignores its answer', async () => {
    let resolve!: (value: ConfigRestorePreview) => void;
    api.previewConfigRestore.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const { component } = mount();
    await unlock();
    await waitFor(() => expect(api.previewConfigRestore).toHaveBeenCalled());
    const signal = vi.mocked(api.previewConfigRestore).mock.calls[0][1]!;
    expect(signal.aborted).toBe(false);
    component.unmount();
    expect(signal.aborted).toBe(true);
    resolve(restorePreview({ credentials }));
    await Promise.resolve();
    expect(screen.queryByText('Email password')).toBeNull();
  });
});
