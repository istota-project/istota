import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { configField, configPreview, configRequest, configView } from '$lib/test/adminConfig';
import type { AdminConfigView, ConfigRequest, User } from '$lib/api';

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
          {
            name: 'anthropic_api_key',
            label: 'Anthropic API key',
            config_key: null,
            set: false,
            read_only_reason: null,
            risky: false,
          },
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
