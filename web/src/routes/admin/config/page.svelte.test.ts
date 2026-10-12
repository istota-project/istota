import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { configField, configPreview, configView } from '$lib/test/adminConfig';
import type { AdminConfigView, User } from '$lib/api';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

import Harness from '$lib/currentUserHarness.test.svelte';
import Page from './+page.svelte';

const user = { username: 'alice', display_name: 'Alice', is_admin: true } as User;

function mount(view: AdminConfigView) {
  api.getAdminConfig.mockResolvedValue(view);
  render(Harness, { props: { user, component: Page } });
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
  it('counts edits in the footer and reviews them as one change set', async () => {
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
    expect(screen.getByText('2 changes')).toBeInTheDocument();

    api.previewConfigChange.mockResolvedValue(configPreview());
    await fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
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
    await fireEvent.input(await screen.findByLabelText('imap_host'), {
      target: { value: 'new' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    expect(screen.queryByText('1 change')).toBeNull();
    expect((screen.getByLabelText('imap_host') as HTMLInputElement).value).toBe('old');
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
