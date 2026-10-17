import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { page } from '../../../../vitest-stubs/app-state.svelte';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import {
  configField,
  configPreview,
  configRequest,
  configSecret,
  configView,
} from '$lib/test/adminConfig';
import type {
  AdminConfigField,
  AdminConfigView,
  AdminServarrConnection,
  ConfigProblem,
  ConfigRequest,
  ConfigSecretFile,
  User,
} from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import Harness from '$lib/currentUserHarness.test.svelte';
import Page from './+page.svelte';
import Layout from '../+layout.svelte';

const user = { username: 'alice', display_name: 'Alice', is_admin: true } as User;

function mount(view: AdminConfigView, history: ConfigRequest[] = []) {
  api.getAdminConfig.mockResolvedValue(view);
  api.listConfigRequests.mockResolvedValue(history);
  render(Harness, { props: { user, component: Page, layout: Layout } });
}

const imapHost = configField({ path: ['email', 'imap_host'], value: 'old', in_file: true });

beforeEach(() => {
  page.url = new URL('http://localhost/istota/admin/config');
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('banners', () => {
  it('says why the page is read-only when there is no applier', async () => {
    mount(
      configView([imapHost], {
        editable: false,
        applier: {
          kind: 'none',
          available: false,
          reason: 'This install has no config applier; changes are made on the host.',
        },
      }),
    );
    expect(await screen.findByText('Read-only: changes are made on the host')).toBeInTheDocument();
    await fireEvent.click(screen.getByText('Read-only: changes are made on the host'));
    expect(screen.getByText(/has no config applier/)).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'imap_host' })).toBeNull();
    expect(screen.getByText('old')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review changes' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Discard' })).toBeNull();
  });

  it('names the plan that manages a resource', async () => {
    const view = configView([imapHost]);
    view.managed.secrets = { managed: true, source: 'ansible', at: null };
    mount(view);
    expect(
      await screen.findByText('The deployment credentials is managed by ansible'),
    ).toBeInTheDocument();
    expect(screen.getByText(/next run of ansible would revert/)).toBeInTheDocument();
  });

  it('lists the problems the current file has to fix first', async () => {
    mount(
      configView([imapHost], {
        problems: [
          {
            kind: 'discarded',
            path: ['scheduler', 'talk_poll_timeout'],
            message: 'must be positive',
            blocking: true,
          },
        ],
      }),
    );
    expect(await screen.findByText('config.toml has problems to fix first')).toBeInTheDocument();
    expect(screen.getByText('scheduler.talk_poll_timeout')).toBeInTheDocument();
  });
});

describe('the change set', () => {
  it('counts edits in the app bar and reviews them as one change set', async () => {
    const secret = configField({
      path: ['email', 'imap_password'],
      secret: true,
      set: true,
      value: null,
      editor: 'secret',
      credential: { kind: 'file', name: 'istota_email_imap_password' },
    });
    mount(configView([imapHost, secret]));
    await fireEvent.input(await screen.findByLabelText('imap_host'), {
      target: { value: 'new' },
    });
    await fireEvent.input(screen.getByLabelText('imap_password'), { target: { value: 'pw' } });
    const header = document.querySelector('.header-tools') as HTMLElement;
    expect(header).not.toBeNull();
    expect(within(header).getByText('2 changes')).toBeInTheDocument();
    expect(within(header).getByRole('button', { name: 'Discard' })).toBeInTheDocument();

    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(within(header).getByRole('button', { name: 'Review changes' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenCalledWith({
        base_digest: 'digest-1',
        ops: [{ op: 'set', path: ['email', 'imap_host'], value: 'new' }],
        raw: null,
        secrets: { istota_email_imap_password: 'pw' },
      }),
    );
  });

  it('discards the change set', async () => {
    mount(configView([imapHost]));
    await screen.findByLabelText('imap_host');
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Discard' })).toBeDisabled();
    await fireEvent.input(await screen.findByLabelText('imap_host'), {
      target: { value: 'new' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    expect(screen.queryByText('1 change')).toBeNull();
    expect((screen.getByLabelText('imap_host') as HTMLInputElement).value).toBe('old');
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Discard' })).toBeDisabled();
  });

  it('reloads on a changed file and keeps the edits that still apply', async () => {
    const port = configField({ path: ['web', 'port'], value: 8766, editor: 'int' });
    mount(configView([imapHost, port]));
    await fireEvent.input(await screen.findByLabelText('imap_host'), {
      target: { value: 'new' },
    });
    await fireEvent.input(screen.getByLabelText('port'), { target: { value: '9000' } });

    api.previewConfigChange.mockRejectedValue(
      new api.ConfigChangeError('changed', 409, 'config_changed', { digest: 'digest-2' }),
    );
    api.getAdminConfig.mockResolvedValue(
      configView(
        [
          imapHost,
          configField({
            path: ['web', 'port'],
            value: 8766,
            editor: 'readonly',
            read_only_reason: 'host',
          }),
        ],
        { document_digest: 'digest-2' },
      ),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    expect(await screen.findByText(/1 of your changes no longer apply/)).toBeInTheDocument();
    expect(screen.getByText('1 change')).toBeInTheDocument();
    expect((screen.getByLabelText('imap_host') as HTMLInputElement).value).toBe('new');
  });

  it('edits a credential no config key reads', async () => {
    mount(
      configView([imapHost], {
        secrets: [
          configSecret({
            name: 'anthropic_api_key',
            label: 'Anthropic API key',
            config_key: null,
          }),
        ],
      }),
    );
    await fireEvent.input(await screen.findByLabelText(/^Anthropic API key/), {
      target: { value: 'k' },
    });
    expect(screen.getByText('1 change')).toBeInTheDocument();
  });
});

describe('the current file', () => {
  it('warns that applying drops the comments', async () => {
    mount(configView([imapHost], { has_comments: true }));
    expect(await screen.findByText('config.toml has comments')).toBeInTheDocument();
  });

  it('removes an unknown key and a credential also set inline', async () => {
    mount(
      configView([imapHost], {
        problems: [
          {
            kind: 'unknown',
            path: ['web', 'typo_key'],
            message: '[config] web.typo_key is not a recognised key and is ignored',
            blocking: false,
            action: 'unset',
          },
          {
            kind: 'warning',
            path: ['email', 'imap_password'],
            message: 'email.imap_password is also set in config.toml, where it is ignored',
            blocking: false,
            action: 'unset',
          },
          { kind: 'retired', path: ['old'], message: 'retired, inside a list', blocking: false },
        ],
      }),
    );
    expect(await screen.findByText('Warnings about config.toml')).toBeInTheDocument();
    expect(screen.getByText(/inside a list/)).toBeInTheDocument();
    const removes = screen.getAllByRole('button', { name: 'Remove' });
    expect(removes).toHaveLength(2);
    await fireEvent.click(removes[0]);
    await fireEvent.click(removes[1]);
    expect(screen.getByText('2 changes')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Keep' })).toHaveLength(2);

    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenCalledWith(
        expect.objectContaining({
          ops: [
            { op: 'unset', path: ['web', 'typo_key'] },
            { op: 'unset', path: ['email', 'imap_password'] },
          ],
          raw: null,
        }),
      ),
    );
  });

  it('moves a credential held only inline into its secret file', async () => {
    mount(
      configView([imapHost], {
        problems: [
          {
            kind: 'warning',
            path: ['sms', 'twilio', 'auth_token'],
            message: 'sms.twilio.auth_token is stored in config.toml in plain text',
            blocking: false,
            action: 'move',
          },
        ],
      }),
    );
    await fireEvent.click(await screen.findByRole('button', { name: 'Move to secret file' }));
    expect(screen.getByText('1 change')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Keep in config.toml' })).toBeInTheDocument();

    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalled());
    const sent = api.previewConfigChange.mock.calls[0][0];
    expect(sent.move_to_file).toEqual(['sms.twilio.auth_token']);
    expect(sent.ops).toEqual([]);
    expect(sent.secrets).toEqual({});
  });
});

describe('the raw editor', () => {
  it('locks the form while it holds the file and shows a parse error from the review', async () => {
    mount(configView([imapHost], { raw_toml: 'bot_name = "Filebot"\n' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Edit as text' }));
    await fireEvent.input(screen.getByLabelText(/^config\.toml/), {
      target: { value: 'bot_name = = "x"\n' },
    });
    expect(screen.getByText('1 change')).toBeInTheDocument();
    // The form shows values only while the whole file is pending.
    expect(screen.queryByLabelText('imap_host')).toBeNull();

    api.previewConfigChange.mockRejectedValue(
      new api.ConfigChangeError('The change cannot be read.', 400, 'invalid_change', {
        errors: ['the TOML does not parse: Invalid value (at line 1, column 12)'],
        problems: [],
      }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    expect(await screen.findByText(/at line 1, column 12/)).toBeInTheDocument();
    expect(api.previewConfigChange).toHaveBeenCalledWith(
      expect.objectContaining({ ops: [], raw: 'bot_name = = "x"\n' }),
    );
  });

  it('cannot be opened while the form holds changes', async () => {
    mount(configView([imapHost]));
    await fireEvent.input(await screen.findByLabelText('imap_host'), {
      target: { value: 'new' },
    });
    expect(screen.getByRole('button', { name: 'Edit as text' })).toBeDisabled();
  });
});

describe('history', () => {
  it('follows a request that is already open when the page loads', async () => {
    api.getConfigRequest.mockResolvedValue(configRequest({ id: 21, status: 'taken' }));
    mount(configView([imapHost]), [
      configRequest({ id: 21, status: 'taken' }),
      configRequest({ id: 20, status: 'applied' }),
    ]);
    expect(await screen.findByText('Change #21')).toBeInTheDocument();
    await waitFor(() => expect(api.getConfigRequest).toHaveBeenCalledWith(21));
    await fireEvent.input(screen.getByLabelText('imap_host'), {
      target: { value: 'new' },
    });
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Discard' })).toBeEnabled();
  });

  it('restores an applied change through preview', async () => {
    mount(configView([imapHost]), [
      configRequest({ id: 12, status: 'applied', document_toml: 'bot_name = "Old"\n' }),
    ]);
    api.restoreConfigRequest.mockResolvedValue({
      change_set: { base_digest: 'digest-1', ops: [], raw: 'bot_name = "Old"\n', secrets: {} },
      preview: configPreview({
        diff: ['--- config.toml', '+++ config.toml', '-bot_name = "New"', '+bot_name = "Old"'],
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
      }),
      note: 'Restoring brings back the configuration only.',
    });
    await fireEvent.click(await screen.findByRole('button', { name: 'Restore' }));
    expect(api.restoreConfigRequest).toHaveBeenCalledWith(12);
    expect(await screen.findByText(/\+bot_name = "Old"/)).toBeInTheDocument();
    expect(screen.getByText(/was cleared after this change/)).toBeInTheDocument();
    expect(
      screen.getAllByText(/Restoring change #12. Restoring brings back the configuration only./)
        .length,
    ).toBeGreaterThan(0);
    expect(api.previewConfigChange).not.toHaveBeenCalled();

    // Closing the review keeps the restored file as the pending change.
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByText('1 change')).toBeInTheDocument();
    expect((screen.getByLabelText(/^config\.toml/) as HTMLTextAreaElement).value).toBe(
      'bot_name = "Old"\n',
    );
    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenCalledWith({
        base_digest: 'digest-1',
        ops: [],
        raw: 'bot_name = "Old"\n',
        secrets: {},
      }),
    );
  });
});

const googleFields = [
  configField({
    path: ['google_workspace', 'enabled'],
    value: false,
    default: false,
    type: 'bool',
    editor: 'bool',
  }),
  configField({ path: ['google_workspace', 'client_id'] }),
  configField({
    path: ['google_workspace', 'client_secret'],
    secret: true,
    set: true,
    value: null,
    editor: 'secret',
    credential: { kind: 'file', name: 'istota_google_workspace_client_secret' },
  }),
  configField({
    path: ['google_workspace', 'scopes'],
    type: 'list',
    editor: 'list',
    value: ['https://www.googleapis.com/auth/drive.readonly'],
    default: ['https://www.googleapis.com/auth/drive.readonly'],
  }),
];

describe('section navigation', () => {
  it('opens all Google fields, including defaults absent from the file, and can be cleared', async () => {
    page.url = new URL('http://localhost/istota/admin/config?section=google_workspace');
    mount(configView([imapHost, ...googleFields]));
    expect(await screen.findByLabelText('Filter settings')).toHaveValue('google_workspace');
    expect(screen.queryByLabelText('imap_host')).toBeNull();
    expect(screen.getByLabelText('enabled')).not.toBeChecked();
    expect(screen.getByLabelText('client_id')).toHaveValue('');
    expect(screen.getByLabelText('client_secret')).toHaveValue('');
    expect(screen.getByLabelText('scopes')).toHaveAttribute(
      'placeholder',
      'https://www.googleapis.com/auth/drive.readonly',
    );
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled();
    await fireEvent.input(screen.getByLabelText('Filter settings'), { target: { value: '' } });
    expect(screen.getByLabelText('imap_host')).toBeInTheDocument();
    expect(screen.getByLabelText('client_id')).toBeInTheDocument();
  });

  it.each(['', '?section=unknown', '?section=google', '?section=Google_workspace'])(
    'leaves the editor unfiltered for %s',
    async (query) => {
      page.url = new URL(`http://localhost/istota/admin/config${query}`);
      mount(configView([imapHost, ...googleFields]));
      expect(await screen.findByLabelText('Filter settings')).toHaveValue('');
      expect(screen.getByLabelText('imap_host')).toBeInTheDocument();
      expect(screen.getByLabelText('client_id')).toBeInTheDocument();
    },
  );

  it('changes sections on a retained page without losing either part of a draft', async () => {
    page.url = new URL('http://localhost/istota/admin/config?section=google_workspace');
    mount(configView([imapHost, ...googleFields]));
    await fireEvent.input(await screen.findByLabelText('client_id'), {
      target: { value: 'replacement-client' },
    });
    await fireEvent.input(screen.getByLabelText('client_secret'), {
      target: { value: 'replacement-secret' },
    });
    page.url = new URL('http://localhost/istota/admin/config?section=email');
    expect(await screen.findByLabelText('imap_host')).toBeInTheDocument();
    expect(screen.queryByLabelText('client_id')).toBeNull();
    page.url = new URL('http://localhost/istota/admin/config?section=google_workspace');
    expect(await screen.findByLabelText('client_id')).toHaveValue('replacement-client');
    expect(screen.getByLabelText('client_secret')).toHaveValue('replacement-secret');

    // An unrelated URL change must not reapply a hint the admin has cleared.
    await fireEvent.input(screen.getByLabelText('Filter settings'), { target: { value: '' } });
    page.url = new URL('http://localhost/istota/admin/config?section=google_workspace&other=1');
    await waitFor(() => expect(screen.getByLabelText('Filter settings')).toHaveValue(''));
    expect(screen.getByLabelText('imap_host')).toBeInTheDocument();
    expect(screen.getByText('2 changes')).toBeInTheDocument();

    page.url = new URL('http://localhost/istota/admin/config?section=email');
    await waitFor(() => expect(screen.getByLabelText('Filter settings')).toHaveValue('email'));
    page.url = new URL('http://localhost/istota/admin/config?section=unknown');
    await waitFor(() => expect(screen.getByLabelText('Filter settings')).toHaveValue(''));
    page.url = new URL('http://localhost/istota/admin/config?section=google_workspace');
    await waitFor(() =>
      expect(screen.getByLabelText('Filter settings')).toHaveValue('google_workspace'),
    );
    page.url = new URL('http://localhost/istota/admin/config');
    await waitFor(() => expect(screen.getByLabelText('Filter settings')).toHaveValue(''));
    expect(api.getAdminConfig).toHaveBeenCalledTimes(1);
  });

  it('keeps the edited filter when a review conflict reloads configuration', async () => {
    page.url = new URL('http://localhost/istota/admin/config?section=google_workspace');
    mount(configView([imapHost, ...googleFields]));
    await fireEvent.input(await screen.findByLabelText('client_id'), {
      target: { value: 'replacement-client' },
    });
    await fireEvent.input(screen.getByLabelText('Filter settings'), {
      target: { value: 'client' },
    });
    api.previewConfigChange.mockRejectedValue(
      new api.ConfigChangeError('changed', 409, 'config_changed', { digest: 'digest-2' }),
    );
    api.getAdminConfig.mockResolvedValue(
      configView([imapHost, ...googleFields], { document_digest: 'digest-2' }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await screen.findByText(/your changes are kept/);
    expect(screen.getByLabelText('Filter settings')).toHaveValue('client');
    expect(screen.getByLabelText('client_id')).toHaveValue('replacement-client');
  });
});

describe('Google configuration review', () => {
  beforeEach(() => {
    page.url = new URL('http://localhost/istota/admin/config?section=google_workspace');
  });

  it('reviews the client ID and replacement secret together without submitting them', async () => {
    mount(configView([imapHost, ...googleFields]));
    await fireEvent.input(await screen.findByLabelText('client_id'), {
      target: { value: 'replacement-client' },
    });
    await fireEvent.input(screen.getByLabelText('client_secret'), {
      target: { value: 'replacement-secret' },
    });
    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenCalledWith({
        base_digest: 'digest-1',
        ops: [{ op: 'set', path: ['google_workspace', 'client_id'], value: 'replacement-client' }],
        raw: null,
        secrets: { istota_google_workspace_client_secret: 'replacement-secret' },
      }),
    );
    expect(api.submitConfigChange).not.toHaveBeenCalled();
  });

  it('leaves an untouched secret unchanged and requires an explicit clear', async () => {
    mount(configView(googleFields));
    await fireEvent.input(await screen.findByLabelText('client_id'), {
      target: { value: 'replacement-client' },
    });
    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ secrets: {} }),
      ),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Clear stored client_secret' }));
    expect(screen.getByText('Will be cleared.')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ secrets: { istota_google_workspace_client_secret: '' } }),
      ),
    );
  });

  it('keeps Google settings read-only without a host applier', async () => {
    mount(
      configView(googleFields, {
        editable: false,
        applier: { kind: 'none', available: false, reason: 'This install has no config applier.' },
      }),
    );
    await fireEvent.click(await screen.findByText('Read-only: changes are made on the host'));
    expect(screen.getByText('This install has no config applier.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '[google_workspace]' })).toBeInTheDocument();
    expect(screen.queryByLabelText('client_id')).toBeNull();
    expect(screen.queryByLabelText('client_secret')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review changes' })).toBeNull();
  });

  it.each(['environment', 'managed'] as const)(
    'keeps %s Google fields read-only',
    async (source) => {
      const reason = source === 'environment' ? 'Set by the environment.' : 'Managed by ansible.';
      const view = configView(
        googleFields.map((field) => ({ ...field, editor: 'readonly', read_only_reason: reason })),
      );
      if (source === 'managed') {
        view.managed.config = { managed: true, source: 'ansible', at: null };
        view.managed.secrets = { managed: true, source: 'ansible', at: null };
      }
      mount(view);
      expect((await screen.findAllByText(reason)).length).toBe(4);
      expect(screen.queryByLabelText('client_id')).toBeNull();
      expect(screen.queryByLabelText('client_secret')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Clear stored client_secret' })).toBeNull();
      expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled();
      expect(api.previewConfigChange).not.toHaveBeenCalled();
    },
  );
});

function credentialField(path: string[], set: boolean) {
  return configField({
    path,
    secret: true,
    set,
    value: null,
    editor: 'secret',
    credential: { kind: 'file', name: `istota_${path.join('_')}` },
  });
}

function fileSecret(path: string[], overrides: Partial<ConfigSecretFile> = {}): ConfigSecretFile {
  return configSecret({
    name: `istota_${path.join('_')}`,
    label: path.join('.'),
    config_key: path.join('.'),
    ...overrides,
  });
}

function moveProblem(path: string[]): ConfigProblem {
  return {
    kind: 'warning',
    path,
    message: `${path.join('.')} is stored in config.toml in plain text`,
    blocking: false,
    action: 'move',
  };
}

const imapPath = ['email', 'imap_password'];
const smtpPath = ['email', 'smtp_password'];
const twilioPath = ['sms', 'twilio', 'auth_token'];
const nativePath = ['brain', 'native', 'api_key'];

function fieldRow(label: string): HTMLElement {
  return screen.getByText(label).closest('.config-field') as HTMLElement;
}

describe('credential status', () => {
  it('badges each credential field with where its value is held', async () => {
    mount(
      configView(
        [
          credentialField(imapPath, true),
          credentialField(smtpPath, false),
          credentialField(twilioPath, true),
          credentialField(nativePath, true),
        ],
        {
          secrets: [
            fileSecret(imapPath, { set: true, source: 'file' }),
            fileSecret(smtpPath),
            fileSecret(twilioPath, { set: true, source: 'inline' }),
            fileSecret(nativePath, { set: true, source: 'environment' }),
            configSecret({
              name: 'anthropic_api_key',
              label: 'Anthropic API key',
              config_key: null,
              set: true,
              source: 'file',
            }),
          ],
        },
      ),
    );
    await screen.findByLabelText('imap_password');
    expect(within(fieldRow('imap_password')).getByText('Set')).toBeInTheDocument();
    expect(within(fieldRow('smtp_password')).getByText('Not set')).toBeInTheDocument();
    expect(within(fieldRow('auth_token')).getByText('In config.toml')).toBeInTheDocument();
    expect(within(fieldRow('api_key')).getByText('Set outside the stack')).toBeInTheDocument();
    const standalone = screen.getByText('Anthropic API key').closest('.standalone-row');
    expect(within(standalone as HTMLElement).getByText('Set')).toBeInTheDocument();
  });

  it('keeps the badge on a read-only page, with no input', async () => {
    mount(
      configView([credentialField(twilioPath, true)], {
        editable: false,
        applier: { kind: 'none', available: false, reason: 'No applier.' },
        secrets: [fileSecret(twilioPath, { set: true, source: 'inline' })],
      }),
    );
    expect(await screen.findByText('In config.toml')).toBeInTheDocument();
    expect(screen.queryByLabelText('auth_token')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Move all to secret files' })).toBeNull();
  });
});

describe('credentials held in config.toml', () => {
  const inlineView = (overrides: Partial<AdminConfigView> = {}) =>
    configView([imapHost, credentialField(twilioPath, true), credentialField(smtpPath, true)], {
      secrets: [
        fileSecret(twilioPath, { set: true, source: 'inline' }),
        fileSecret(smtpPath, { set: true, source: 'inline' }),
        fileSecret(imapPath, { set: true, source: 'file' }),
      ],
      problems: [moveProblem(twilioPath), moveProblem(smtpPath)],
      ...overrides,
    });

  it('counts them and moves them all by label, with nothing under secrets', async () => {
    mount(inlineView());
    expect(
      await screen.findByText('2 credentials are stored in config.toml in plain text'),
    ).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Move all to secret files' }));
    expect(screen.getByText('2 changes')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Keep in config.toml' })).toHaveLength(2);

    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalled());
    const sent = api.previewConfigChange.mock.calls[0][0];
    expect(sent.move_to_file).toEqual(['sms.twilio.auth_token', 'email.smtp_password']);
    expect(sent.secrets).toEqual({});
    expect(sent.ops).toEqual([]);
  });

  it('takes the moves back with Keep all', async () => {
    mount(inlineView());
    await fireEvent.click(await screen.findByRole('button', { name: 'Move all to secret files' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Keep all in config.toml' }));
    expect(screen.getByRole('button', { name: 'Move all to secret files' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled();
  });

  it('shows no notice when every credential is in its file', async () => {
    mount(
      configView([imapHost], { secrets: [fileSecret(imapPath, { set: true, source: 'file' })] }),
    );
    await screen.findByLabelText('imap_host');
    expect(screen.queryByText(/stored in config.toml in plain text/)).toBeNull();
  });

  it('says why none can move when a plan manages the credentials', async () => {
    const view = inlineView({
      secrets: [
        fileSecret(twilioPath, { set: true, source: 'inline', movable: false }),
        fileSecret(smtpPath, { set: true, source: 'inline', movable: false }),
      ],
      problems: [],
    });
    view.managed.secrets = { managed: true, source: 'ansible', at: null };
    mount(view);
    expect(
      await screen.findByText(
        /None of them can be moved from here, because a plan \(ansible\) manages the deployment credentials/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Move all to secret files' })).toBeNull();
  });

  it('keeps the moves that still apply when the file changed underneath', async () => {
    mount(inlineView());
    await fireEvent.click(await screen.findByRole('button', { name: 'Move all to secret files' }));
    api.previewConfigChange.mockRejectedValue(
      new api.ConfigChangeError('changed', 409, 'config_changed', { digest: 'digest-2' }),
    );
    // The Twilio token was moved by someone else meanwhile.
    api.getAdminConfig.mockResolvedValue(
      inlineView({
        document_digest: 'digest-2',
        secrets: [
          fileSecret(twilioPath, { set: true, source: 'file' }),
          fileSecret(smtpPath, { set: true, source: 'inline' }),
        ],
        problems: [moveProblem(smtpPath)],
      }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    expect(await screen.findByText(/1 of your changes no longer apply/)).toBeInTheDocument();
    expect(screen.getByText('1 change')).toBeInTheDocument();

    api.previewConfigChange.mockResolvedValue(configPreview({ base_digest: 'digest-2' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() =>
      expect(api.previewConfigChange).toHaveBeenLastCalledWith({
        base_digest: 'digest-2',
        ops: [],
        raw: null,
        secrets: {},
        move_to_file: ['email.smtp_password'],
      }),
    );
  });
});

describe('the [servarr] apps editor', () => {
  const RADARR_FILE = 'istota_servarr_radarr_api_key';

  function servarrConnection(over: Partial<AdminServarrConnection> = {}): AdminServarrConnection {
    return {
      id: 'servarr',
      label: 'Servarr',
      status: 'configured',
      apps: [
        {
          app: 'radarr',
          url: 'http://10.0.0.5:7878',
          api_key_set: true,
          incomplete: false,
          problem: null,
          check: null,
        },
      ],
      missing: ['sonarr', 'lidarr', 'prowlarr'],
      requesters: [],
      defaults: {},
      ...over,
    };
  }

  const NO_APPS: Partial<AdminServarrConnection> = {
    status: 'disabled',
    apps: [],
    missing: ['sonarr', 'radarr', 'lidarr', 'prowlarr'],
  };

  function keyFiles(readOnlyReason: string | null = null): ConfigSecretFile[] {
    return ['sonarr', 'radarr', 'lidarr', 'prowlarr'].map((name) =>
      configSecret({
        name: `istota_servarr_${name}_api_key`,
        label: `servarr.apps.${name}.api_key`,
        config_key: `servarr.apps.${name}.api_key`,
        set: name === 'radarr',
        source: name === 'radarr' ? 'file' : 'none',
        read_only_reason: readOnlyReason,
      }),
    );
  }

  function servarrFields(appsOver: Partial<AdminConfigField> = {}) {
    return [
      configField({
        path: ['servarr', 'apps'],
        type: 'dict',
        editor: 'table',
        value: { radarr: null },
        in_file: true,
        ...appsOver,
      }),
      configField({
        path: ['servarr', 'private_hosts'],
        type: 'list',
        editor: 'list',
        value: ['10.0.0.5'],
        default: [],
        in_file: true,
      }),
    ];
  }

  function mountServarr(
    over: Partial<AdminServarrConnection> = {},
    viewOver: Partial<AdminConfigView> = {},
    appsOver: Partial<AdminConfigField> = {},
  ) {
    api.getAdminConnections.mockResolvedValue({ connections: [servarrConnection(over)] });
    mount(configView([imapHost, ...servarrFields(appsOver)], { secrets: keyFiles(), ...viewOver }));
  }

  async function block(app: string) {
    return within(await screen.findByTestId(`servarr-app-${app}`));
  }

  async function review() {
    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalled());
    return api.previewConfigChange.mock.calls.at(-1)?.[0];
  }

  it('shows a block per app with its URL and key status, and no pointer to Connections', async () => {
    mountServarr();
    const radarr = await block('radarr');
    expect(radarr.getByLabelText<HTMLInputElement>(/^URL/).value).toBe('http://10.0.0.5:7878');
    expect(radarr.getByLabelText<HTMLInputElement>(/^API key/).value).toBe('');
    expect(radarr.getByText('Set')).toBeInTheDocument();
    expect((await block('sonarr')).getByText('Not connected.')).toBeInTheDocument();
    expect(screen.queryByText(/card in Connections has a form/)).toBeNull();
    expect(screen.queryByRole('link', { name: 'Open Connections' })).toBeNull();
  });

  it('sends a changed URL as an op and a new key under its file name', async () => {
    mountServarr();
    const radarr = await block('radarr');
    await fireEvent.input(radarr.getByLabelText(/^URL/), {
      target: { value: 'http://10.0.0.5:7879' },
    });
    await fireEvent.input(radarr.getByLabelText(/^API key/), {
      target: { value: 'fixture-arr-key-9' },
    });
    expect(screen.getByText('2 changes')).toBeInTheDocument();
    const sent = await review();
    expect(sent.ops).toEqual([
      { op: 'set', path: ['servarr', 'apps', 'radarr', 'url'], value: 'http://10.0.0.5:7879' },
    ]);
    expect(sent.secrets).toEqual({ [RADARR_FILE]: 'fixture-arr-key-9' });
  });

  it('removes the table of an app turned off and clears its key file', async () => {
    mountServarr();
    await fireEvent.click((await block('radarr')).getByLabelText('Connected'));
    const sent = await review();
    expect(sent.ops).toEqual([{ op: 'unset', path: ['servarr', 'apps', 'radarr'] }]);
    expect(sent.secrets).toEqual({ [RADARR_FILE]: '' });
  });

  it('adds a first app on an install with none (ISSUE-767)', async () => {
    mountServarr(NO_APPS);
    const sonarr = await block('sonarr');
    await fireEvent.click(sonarr.getByLabelText('Connected'));
    const url = sonarr.getByLabelText<HTMLInputElement>(/^URL/);
    expect(url.placeholder).toBe('http://192.168.1.10:8989');
    await fireEvent.input(url, { target: { value: 'https://sonarr.example.com' } });
    await fireEvent.input(sonarr.getByLabelText(/^API key/), {
      target: { value: 'fixture-arr-key-1' },
    });
    const sent = await review();
    expect(sent.ops).toEqual([
      {
        op: 'set',
        path: ['servarr', 'apps', 'sonarr', 'url'],
        value: 'https://sonarr.example.com',
      },
    ]);
    expect(sent.secrets).toEqual({ istota_servarr_sonarr_api_key: 'fixture-arr-key-1' });
  });

  it('refuses plain HTTP to a host not in the private hosts before any preview', async () => {
    mountServarr();
    const radarr = await block('radarr');
    await fireEvent.input(radarr.getByLabelText(/^URL/), {
      target: { value: 'http://10.0.0.9:7878' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    expect(api.previewConfigChange).not.toHaveBeenCalled();
    expect(radarr.getAllByText(/not in the private hosts/).length).toBeGreaterThan(0);

    await fireEvent.click(radarr.getByRole('button', { name: 'Add 10.0.0.9 to private hosts' }));
    expect(radarr.queryByText(/not in the private hosts/)).toBeNull();
    const sent = await review();
    expect(sent.ops).toContainEqual({
      op: 'set',
      path: ['servarr', 'private_hosts'],
      value: ['10.0.0.5', '10.0.0.9'],
    });
  });

  it('shows a read-only key with its reason and no input', async () => {
    const reason = 'Managed by ansible. Change it there.';
    mountServarr({}, { secrets: keyFiles(reason) });
    const radarr = await block('radarr');
    expect(radarr.getByText(reason)).toBeInTheDocument();
    expect(radarr.getByText('Set')).toBeInTheDocument();
    expect(radarr.queryByLabelText(/^API key/)).toBeNull();
  });

  it('is read-only with the reason when a plan manages config.toml', async () => {
    const reason = 'config.toml is managed by ansible.';
    mountServarr({}, {}, { editor: 'readonly', read_only_reason: reason });
    const radarr = await block('radarr');
    expect(radarr.getByText('http://10.0.0.5:7878')).toBeInTheDocument();
    expect(radarr.queryByLabelText(/^URL/)).toBeNull();
    expect(radarr.queryByLabelText('Connected')).toBeNull();
    expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
  });

  it('keeps the app edits when config.toml changed underneath', async () => {
    mountServarr();
    const radarr = await block('radarr');
    await fireEvent.input(radarr.getByLabelText(/^URL/), {
      target: { value: 'http://10.0.0.5:7879' },
    });
    api.previewConfigChange.mockRejectedValueOnce(
      new api.ConfigChangeError('changed', 409, 'config_changed', { digest: 'digest-2' }),
    );
    api.getAdminConfig.mockResolvedValue(
      configView([imapHost, ...servarrFields()], {
        secrets: keyFiles(),
        document_digest: 'digest-2',
      }),
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    expect(await screen.findByText(/your changes are kept/)).toBeInTheDocument();
    expect((await block('radarr')).getByLabelText<HTMLInputElement>(/^URL/).value).toBe(
      'http://10.0.0.5:7879',
    );
    const sent = await review();
    expect(sent).toMatchObject({
      base_digest: 'digest-2',
      ops: [
        { op: 'set', path: ['servarr', 'apps', 'radarr', 'url'], value: 'http://10.0.0.5:7879' },
      ],
    });
  });

  it('discards the app edits with the rest of the change', async () => {
    mountServarr();
    const radarr = await block('radarr');
    await fireEvent.input(radarr.getByLabelText(/^URL/), {
      target: { value: 'http://10.0.0.5:7879' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    expect(radarr.getByLabelText<HTMLInputElement>(/^URL/).value).toBe('http://10.0.0.5:7878');
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled();
  });
});
