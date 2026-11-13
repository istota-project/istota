import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { configView } from '$lib/test/adminConfig';

vi.mock('$app/paths', () => ({ base: '/istota' }));
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
const copyText = vi.hoisted(() => vi.fn(async () => true));
vi.mock('@minot/ui/clipboard', () => ({ copyText }));

import { ConfigChangeError, type ConfigBackupExport } from '$lib/api';
import BackupDialog from './BackupDialog.svelte';

const password = 'fixture-backup-password';
const preamble = '# Istota configuration backup.\n\n';

/** Every download: its name and the blob handed to the browser. */
const downloads: { name: string; blob: Blob }[] = [];
let lastBlob: Blob | null = null;

function exported(overrides: Partial<ConfigBackupExport['summary']> = {}): ConfigBackupExport {
  return {
    filename: 'istota-config-20261113T091500Z.kdbx',
    file: btoa('encrypted'),
    password,
    summary: {
      file_credentials: 2,
      inline_credentials: 1,
      omissions: [
        { category: 'environment', name: 'istota_brave_api_key', reason: 'supplied outside' },
      ],
      secrets_dir_available: true,
      ...overrides,
    },
  };
}

beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal(
    'URL',
    Object.assign(URL, {
      createObjectURL: vi.fn((blob: Blob) => {
        lastBlob = blob;
        return 'blob:fixture';
      }),
      revokeObjectURL: vi.fn(),
    }),
  );
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push({ name: this.download, blob: lastBlob! });
  });
  api.startStepUp.mockResolvedValue({
    request_id: 'request',
    method: 'email' as const,
    email_hint: 'a•••@example.com',
    expires_at: 'later',
  });
  api.exportConfigBackup.mockResolvedValue(exported());
  api.getAdminConfig.mockResolvedValue(
    configView([], {
      raw_toml: '[web]\nport = 8080\n',
      backup_limits: { config_bytes: 1000, config_preamble: preamble },
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  downloads.length = 0;
  lastBlob = null;
});

function mount(props: Record<string, unknown> = {}) {
  const onClose = vi.fn();
  const component = render(BackupDialog, {
    props: { onClose, encryptedAvailable: true, limitsAvailable: true, ...props },
  });
  return { onClose, component };
}

function secretsBox() {
  return screen.getByLabelText('Include installation secrets', { exact: false, selector: 'input' });
}

async function confirmCode() {
  await screen.findByText(/a•••@example.com/);
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
}

describe('the plain download', () => {
  it('reads the saved file afresh and downloads it with the preamble', async () => {
    const { onClose } = mount();
    expect(api.getAdminConfig).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(api.getAdminConfig).toHaveBeenCalledTimes(1);
    expect(downloads[0].name).toMatch(/^istota-config-\d{8}T\d{6}Z\.toml$/);
    expect(await downloads[0].blob.text()).toBe(`${preamble}[web]\nport = 8080\n`);
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(api.exportConfigBackup).not.toHaveBeenCalled();
    expect(api.startStepUp).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('refuses a file over the server limit before making a blob, and stays to retry', async () => {
    api.getAdminConfig.mockResolvedValue(
      configView([], {
        raw_toml: `x = "${'é'.repeat(600)}"\n`,
        backup_limits: { config_bytes: 1000, config_preamble: preamble },
      }),
    );
    const { onClose } = mount();
    await fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /over the backup limit of 1000 bytes\. Nothing was downloaded\./,
    );
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(downloads).toHaveLength(0);
    expect(onClose).not.toHaveBeenCalled();
    expect(api.getAdminConfig).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled();
  });

  it('does not download when the server gave no limit', async () => {
    mount({ limitsAvailable: false });
    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled();
    expect(screen.getByText(/did not say how large a backup may be/)).toBeInTheDocument();
  });

  it('refuses when the fresh view has lost its limit', async () => {
    api.getAdminConfig.mockResolvedValue(configView([], { backup_limits: null }));
    mount();
    await fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Reload the page/);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('says unsaved edits are not in it, and names what is excluded', () => {
    mount({ unsaved: true });
    expect(screen.getByText(/Unsaved changes are not included/)).toBeInTheDocument();
    expect(screen.getByText(/credential-store master key/)).toBeInTheDocument();
    expect(
      screen.getByText(/keep database\/workspace backups and the original master key separately/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Disaster recovery ready/i)).toBeNull();
  });

  it('links the lost-installation runbook from the options and the saved-password screen', async () => {
    const runbook = /\/docs\/deployment\/running-the-stack\/#recovering-a-lost-installation$/;
    mount();
    const [options] = screen.getAllByRole('link', { name: 'Recovering an installation' });
    expect(options.getAttribute('href')).toMatch(runbook);
    await fireEvent.click(secretsBox());
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await confirmCode();
    await screen.findByText(password);
    const links = screen.getAllByRole('link', { name: 'Recovering an installation' });
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) expect(link.getAttribute('href')).toMatch(runbook);
    expect(
      screen.getAllByText(/keep database\/workspace backups and the original master key separately/)
        .length,
    ).toBeGreaterThan(0);
  });
});

describe('the encrypted backup', () => {
  it('is off at first, and offered only where the server can write it', async () => {
    mount({ encryptedAvailable: false });
    expect(secretsBox()).not.toBeChecked();
    expect(secretsBox()).toBeDisabled();
    expect(screen.getByText(/vault extra is not installed/)).toBeInTheDocument();
    cleanup();
    mount({ requestOpen: true });
    expect(secretsBox()).toBeDisabled();
    expect(screen.getByText(/waiting for the host/)).toBeInTheDocument();
  });

  it('downloads the key file first, then the backup, and holds the password until saved', async () => {
    const { onClose } = mount();
    await fireEvent.click(secretsBox());
    await fireEvent.click(
      screen.getByLabelText('Also require a key file', { exact: false, selector: 'input' }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await screen.findByText(/a•••@example.com/);
    expect(downloads[0].name).toMatch(/^istota-config-.*\.keyx$/);
    const xml = await downloads[0].blob.text();
    expect(xml).toContain('<Version>2.0</Version>');
    expect(api.exportConfigBackup).not.toHaveBeenCalled();
    expect(api.startStepUp).toHaveBeenCalledWith('admin_config_backup', undefined);
    await confirmCode();
    await screen.findByText(password);
    expect(api.exportConfigBackup).toHaveBeenCalledWith(
      btoa(xml),
      { request_id: 'request', code: '123456' },
      expect.any(AbortSignal),
    );
    expect(downloads[1].name).toBe('istota-config-20261113T091500Z.kdbx');
    expect(await downloads[1].blob.text()).toBe('encrypted');
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/2 deployment credentials and 1 credential held/)).toBeInTheDocument();
    expect(screen.getByText('istota_brave_api_key')).toBeInTheDocument();
    expect(screen.getByText(/original master key separately/)).toBeInTheDocument();
    await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(screen.getByText(password)).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Copy password' }));
    expect(copyText).toHaveBeenCalledWith(password, { label: 'Password copied' });
    await fireEvent.click(screen.getByRole('button', { name: 'I saved it' }));
    expect(onClose).toHaveBeenCalled();
  });

  it('sends no key file unless asked, and says when there are no secret files', async () => {
    api.exportConfigBackup.mockResolvedValue(exported({ secrets_dir_available: false }));
    mount();
    await fireEvent.click(secretsBox());
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await confirmCode();
    await screen.findByText(password);
    expect(vi.mocked(api.exportConfigBackup).mock.calls[0][0]).toBeNull();
    expect(downloads.map((d) => d.name)).toEqual(['istota-config-20261113T091500Z.kdbx']);
    expect(screen.getByText(/no deployment secret files/)).toBeInTheDocument();
  });

  it('keeps a refused code in the step-up dialog', async () => {
    api.exportConfigBackup.mockRejectedValueOnce(
      new ConfigChangeError('That code is wrong.', 403, null, { field: 'code', reason: 'bad' }),
    );
    mount();
    await fireEvent.click(secretsBox());
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await confirmCode();
    expect(await screen.findByText('That code is wrong.')).toBeInTheDocument();
    expect(screen.getByLabelText('Confirmation code')).toBeInTheDocument();
    expect(screen.queryByText(password)).toBeNull();
  });

  it('returns a busy or size refusal to the options, kept for a deliberate retry', async () => {
    api.exportConfigBackup.mockRejectedValueOnce(
      new ConfigChangeError('Another export is running.', 503, 'backup_busy'),
    );
    mount();
    await fireEvent.click(secretsBox());
    await fireEvent.click(
      screen.getByLabelText('Also require a key file', { exact: false, selector: 'input' }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await confirmCode();
    expect(await screen.findByRole('alert')).toHaveTextContent('Another export is running.');
    expect(secretsBox()).toBeChecked();
    expect(api.exportConfigBackup).toHaveBeenCalledTimes(1);
    expect(downloads.map((d) => d.name)).toHaveLength(1);
    const sentKey = vi.mocked(api.exportConfigBackup).mock.calls[0][0];
    // The retry sends the key file already saved rather than a second one.
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await confirmCode();
    await screen.findByText(password);
    expect(api.exportConfigBackup).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.exportConfigBackup).mock.calls[1][0]).toBe(sentKey);
    expect(downloads.filter((d) => d.name.endsWith('.keyx'))).toHaveLength(1);
  });

  it('closes on a cancelled step-up and forgets the key file', async () => {
    const { onClose } = mount();
    await fireEvent.click(secretsBox());
    await fireEvent.click(
      screen.getByLabelText('Also require a key file', { exact: false, selector: 'input' }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await screen.findByText(/a•••@example.com/);
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
    expect(api.exportConfigBackup).not.toHaveBeenCalled();
  });

  it('aborts on unmount and ignores a late answer', async () => {
    let resolve!: (value: ConfigBackupExport) => void;
    api.exportConfigBackup.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const { component } = mount();
    await fireEvent.click(secretsBox());
    await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await confirmCode();
    const signal = vi.mocked(api.exportConfigBackup).mock.calls[0][2]!;
    expect(signal.aborted).toBe(false);
    component.unmount();
    expect(signal.aborted).toBe(true);
    const late = exported();
    resolve(late);
    await Promise.resolve();
    await Promise.resolve();
    expect(downloads).toHaveLength(0);
    expect(late.password).toBe('');
    expect(late.file).toBe('');
    expect(screen.queryByText(password)).toBeNull();
  });
});
