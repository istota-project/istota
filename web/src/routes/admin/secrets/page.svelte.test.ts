import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { configPreview, configSecret, configView } from '$lib/test/adminConfig';
import type { AdminConfigView, ConfigSecretFile, User } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

const notices = vi.hoisted(() => ({ notifyWarning: vi.fn() }));
vi.mock('$lib/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/stores/notices')>()),
  notifyWarning: notices.notifyWarning,
}));

import Harness from '$lib/currentUserHarness.test.svelte';
import Page from './+page.svelte';
import Layout from '../+layout.svelte';

const user = { username: 'alice', display_name: 'Alice', is_admin: true } as User;

const twilioToken = configSecret({
  name: 'istota_sms_twilio_auth_token',
  label: 'sms.twilio.auth_token',
  config_key: 'sms.twilio.auth_token',
  used_by: 'SMS (Twilio)',
  set: true,
  source: 'inline',
});
const imapPassword = configSecret({ set: true, source: 'file' });
const smtpPassword = configSecret({
  name: 'istota_email_smtp_password',
  label: 'email.smtp_password',
  config_key: 'email.smtp_password',
});
const nativeKey = configSecret({
  name: 'istota_brain_native_api_key',
  label: 'brain.native.api_key',
  config_key: 'brain.native.api_key',
  used_by: 'Native brain',
  set: true,
  source: 'environment',
  read_only_reason: 'Set by ISTOTA_BRAIN_NATIVE_API_KEY in the environment.',
});
const anthropicKey = configSecret({
  name: 'anthropic_api_key',
  label: 'Anthropic API key',
  config_key: null,
  used_by: 'Claude Code',
});

function viewOf(secrets: ConfigSecretFile[], overrides: Partial<AdminConfigView> = {}) {
  return configView([], { secrets, ...overrides });
}

function mount(view: AdminConfigView) {
  api.getAdminConfig.mockResolvedValue(view);
  api.listConfigRequests.mockResolvedValue([]);
  render(Harness, { props: { user, component: Page, layout: Layout } });
}

function rowOf(label: string): HTMLElement {
  return screen.getByText(label).closest('tr') as HTMLElement;
}

async function review() {
  api.previewConfigChange.mockResolvedValue(configPreview());
  await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
  await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalled());
  return api.previewConfigChange.mock.calls.at(-1)?.[0];
}

beforeEach(() => {
  page.url = new URL('http://localhost/istota/admin/secrets');
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('the list', () => {
  it('is in the admin sidebar between Configuration and Connections', async () => {
    mount(viewOf([imapPassword]));
    await screen.findByText('email.imap_password');
    const links = screen
      .getAllByRole('link')
      .map((a) => a.textContent?.trim())
      .filter((t) => ['Configuration', 'Secrets', 'Connections'].includes(t ?? ''));
    expect(links).toEqual(['Configuration', 'Secrets', 'Connections']);
    expect(screen.getByRole('link', { name: 'Secrets' }).getAttribute('href')).toMatch(
      /\/admin\/secrets$/,
    );
  });

  it('groups the rows by what reads them and says where each is held', async () => {
    mount(viewOf([imapPassword, twilioToken, smtpPassword, nativeKey, anthropicKey]));
    const email = await screen.findByRole('table', { name: 'Email' });
    expect(within(email).getByText('email.imap_password')).toBeInTheDocument();
    expect(within(email).getByText('email.smtp_password')).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'SMS (Twilio)' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Claude Code' })).toBeInTheDocument();

    expect(within(rowOf('email.imap_password')).getByText('Set')).toBeInTheDocument();
    expect(within(rowOf('email.smtp_password')).getByText('Not set')).toBeInTheDocument();
    expect(within(rowOf('sms.twilio.auth_token')).getByText('In config.toml')).toBeInTheDocument();
    expect(
      within(rowOf('brain.native.api_key')).getByText('Set outside the stack'),
    ).toBeInTheDocument();
    expect(within(rowOf('sms.twilio.auth_token')).getByText('istota_sms_twilio_auth_token'));
  });

  it('counts the credentials held in config.toml in a lasting notice', async () => {
    mount(viewOf([imapPassword, twilioToken]));
    expect(await screen.findByText('1 credential is stored in config.toml')).toBeInTheDocument();
  });

  it('shows no such notice when every credential is in its file', async () => {
    mount(viewOf([imapPassword, smtpPassword]));
    await screen.findByText('email.imap_password');
    expect(screen.queryByText(/stored in config.toml/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Move to secret file' })).toBeNull();
  });
});

describe('actions', () => {
  it('replaces a credential under its file name and in no op', async () => {
    mount(viewOf([imapPassword]));
    await fireEvent.click(
      within(await screen.findByRole('table', { name: 'Email' })).getByRole('button', {
        name: 'Replace',
      }),
    );
    await fireEvent.input(screen.getByLabelText(/^New value for email.imap_password/), {
      target: { value: 'new-password' },
    });
    expect(screen.getByText('1 change')).toBeInTheDocument();
    const sent = await review();
    expect(sent.secrets).toEqual({ istota_email_imap_password: 'new-password' });
    expect(sent.ops).toEqual([]);
    expect(sent.move_to_file).toBeUndefined();
  });

  it('clears a credential held in its file', async () => {
    mount(viewOf([imapPassword]));
    await fireEvent.click(await screen.findByRole('button', { name: 'Clear' }));
    expect(screen.getByText('Will be cleared.')).toBeInTheDocument();
    const sent = await review();
    expect(sent.secrets).toEqual({ istota_email_imap_password: '' });
  });

  it('moves an inline credential by label, with nothing under secrets', async () => {
    mount(viewOf([twilioToken]));
    await fireEvent.click(await screen.findByRole('button', { name: 'Move to secret file' }));
    expect(screen.getByRole('button', { name: 'Keep in config.toml' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Replace' })).toBeNull();
    const sent = await review();
    expect(sent.move_to_file).toEqual(['sms.twilio.auth_token']);
    expect(sent.secrets).toEqual({});
    expect(sent.ops).toEqual([]);
  });

  it('shows the move in the review', async () => {
    mount(viewOf([twilioToken]));
    await fireEvent.click(await screen.findByRole('button', { name: 'Move to secret file' }));
    api.previewConfigChange.mockResolvedValue(
      configPreview({
        moves: [{ label: 'sms.twilio.auth_token', file: 'istota_sms_twilio_auth_token' }],
      }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Moved out of config.toml')).toBeInTheDocument();
    expect(within(dialog).getAllByText(/istota_sms_twilio_auth_token/).length).toBeGreaterThan(0);
  });

  it('removes an ignored inline copy of a credential set in its file', async () => {
    mount(
      viewOf([imapPassword], {
        problems: [
          {
            kind: 'warning',
            path: ['email', 'imap_password'],
            message: 'email.imap_password is also set in config.toml, where it is ignored',
            blocking: false,
            action: 'unset',
          },
        ],
      }),
    );
    expect(await screen.findByText(/also holds a copy, which is ignored/)).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Remove copy' }));
    const sent = await review();
    expect(sent.ops).toEqual([{ op: 'unset', path: ['email', 'imap_password'] }]);
    expect(sent.secrets).toEqual({});
  });
});

describe('read-only states', () => {
  it('shows the reason in place of actions for a credential set outside the stack', async () => {
    mount(viewOf([nativeKey]));
    await screen.findByText('brain.native.api_key');
    const row = rowOf('brain.native.api_key');
    expect(within(row).getByText(/Set by ISTOTA_BRAIN_NATIVE_API_KEY/)).toBeInTheDocument();
    expect(within(row).queryByRole('button')).toBeNull();
  });

  it('offers nothing, Move included, when a plan manages the credentials', async () => {
    const reason = 'The deployment credentials are managed by ansible.';
    mount(
      viewOf([
        { ...twilioToken, read_only_reason: reason },
        { ...imapPassword, read_only_reason: reason },
      ]),
    );
    expect((await screen.findAllByText(reason)).length).toBe(2);
    expect(screen.queryByRole('button', { name: 'Move to secret file' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Replace' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Clear' })).toBeNull();
  });

  it('is read-only without a host applier, and says why', async () => {
    mount(
      viewOf([imapPassword, twilioToken], {
        editable: false,
        applier: { kind: 'none', available: false, reason: 'This install has no config applier.' },
      }),
    );
    await fireEvent.click(await screen.findByText('Read-only: changes are made on the host'));
    expect(screen.getByText('This install has no config applier.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Replace' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Move to secret file' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review changes' })).toBeNull();
  });
});

describe('a changed file', () => {
  it('reloads on config_changed and keeps the entries whose rows are still editable', async () => {
    mount(viewOf([imapPassword, twilioToken, smtpPassword]));
    await fireEvent.click(await screen.findByRole('button', { name: 'Move to secret file' }));
    await fireEvent.click(
      within(rowOf('email.imap_password')).getByRole('button', { name: 'Clear' }),
    );
    await fireEvent.click(
      within(rowOf('email.smtp_password')).getByRole('button', { name: 'Replace' }),
    );
    await fireEvent.input(screen.getByLabelText(/^New value for email.smtp_password/), {
      target: { value: 'smtp' },
    });
    expect(screen.getByText('3 changes')).toBeInTheDocument();

    api.previewConfigChange.mockRejectedValue(
      new api.ConfigChangeError('changed', 409, 'config_changed', { digest: 'digest-2' }),
    );
    // The Twilio token was moved by someone else meanwhile, so that move no
    // longer applies; the two others still do.
    api.getAdminConfig.mockResolvedValue(
      viewOf([imapPassword, { ...twilioToken, source: 'file' }, smtpPassword], {
        document_digest: 'digest-2',
      }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() => expect(notices.notifyWarning).toHaveBeenCalled());
    expect(notices.notifyWarning.mock.calls[0][0]).toMatch(/1 of your changes no longer applies/);
    expect(screen.getByText('2 changes')).toBeInTheDocument();
    expect(screen.getByLabelText(/^New value for email.smtp_password/)).toHaveValue('smtp');

    api.previewConfigChange.mockResolvedValue(configPreview({ base_digest: 'digest-2' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenLastCalledWith({
        base_digest: 'digest-2',
        ops: [],
        raw: null,
        secrets: { istota_email_imap_password: '', istota_email_smtp_password: 'smtp' },
      }),
    );
  });
});
