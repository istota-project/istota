import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, fireEvent, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

import type {
  AdminConfigView,
  AdminNextcloudCheck,
  AdminServarrApp,
  AdminServarrConnection,
} from '$lib/api';
import {
  configField,
  configPreview,
  configRequest,
  configSecret,
  configView,
} from '$lib/test/adminConfig';

/**
 * The servarr card on `/admin/connections`, read view.
 *
 * It shows what the config names per arr app and what the daemon's last
 * `servarr.connection.<app>` check found, and offers a fresh doctor run. The
 * payload never carries a key, so the card can only say whether one is set.
 */

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

const doctorRun = vi.hoisted(() => ({ waitForDoctorRun: vi.fn() }));
vi.mock('$lib/doctorRun', () => doctorRun);
const notices = vi.hoisted(() => ({ notifyError: vi.fn() }));
vi.mock('$lib/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/stores/notices')>()),
  notifyError: notices.notifyError,
}));
vi.mock('$app/paths', () => ({ base: '/istota' }));

import Page from './+page.svelte';

function check(over: Partial<AdminNextcloudCheck> = {}): AdminNextcloudCheck {
  return {
    status: 'ok',
    detail: 'radarr 5.9.1.9070 at http://10.0.0.5:7878',
    remedy: '',
    at: new Date(Date.now() - 12 * 60_000).toISOString(),
    stale: false,
    ...over,
  };
}

function app(over: Partial<AdminServarrApp> = {}): AdminServarrApp {
  return {
    app: 'radarr',
    url: 'http://10.0.0.5:7878',
    api_key_set: true,
    incomplete: false,
    problem: null,
    check: check(),
    ...over,
  };
}

function connection(over: Partial<AdminServarrConnection> = {}): AdminServarrConnection {
  return {
    id: 'servarr',
    label: 'Servarr',
    status: 'configured',
    apps: [
      app({
        app: 'sonarr',
        url: 'http://10.0.0.5:8989',
        check: check({ detail: 'sonarr 4.0.9.2244 at http://10.0.0.5:8989' }),
      }),
      app(),
    ],
    missing: ['lidarr', 'prowlarr'],
    requesters: ['bob'],
    defaults: { radarr: { quality_profile: 'HD - 720p/1080p', root_folder: '/films' } },
    ...over,
  };
}

async function mount(over: Partial<AdminServarrConnection> = {}) {
  api.getAdminConnections.mockResolvedValue({ connections: [connection(over)] });
  render(Page);
  return screen.findByTestId('servarr-card');
}

beforeEach(() => {
  api.getAdminConnections.mockReset();
  doctorRun.waitForDoctorRun.mockReset();
  notices.notifyError.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('the servarr card', () => {
  it('shows one row per app with its status and version', async () => {
    const card = await mount();
    const q = within(card);
    expect(q.getByText('Configured')).toBeTruthy();
    const radarr = within(q.getByTestId('servarr-app-radarr'));
    expect(radarr.getByText('OK')).toBeTruthy();
    expect(radarr.getByText('radarr 5.9.1.9070 at http://10.0.0.5:7878')).toBeTruthy();
    expect(radarr.getByText('Set')).toBeTruthy();
    expect(radarr.getByText(/checked 12m ago/)).toBeTruthy();
    const link = radarr.getByRole('link', { name: 'http://10.0.0.5:7878' });
    expect(link.getAttribute('target')).toBe('_blank');
    const sonarr = within(q.getByTestId('servarr-app-sonarr'));
    expect(sonarr.getByText(/sonarr 4\.0\.9\.2244/)).toBeTruthy();
    expect(q.getByTestId('servarr-missing-lidarr')).toHaveTextContent('Not configured');
    expect(q.getByTestId('servarr-missing-prowlarr')).toHaveTextContent('Not configured');
    expect(q.getByText('bob')).toBeTruthy();
    expect(q.getByText('quality profile: HD - 720p/1080p · root folder: /films')).toBeTruthy();
    expect(q.queryByText(/None of the apps passed/)).toBeNull();
  });

  it('shows a missing key and a config problem', async () => {
    const card = await mount({
      status: 'incomplete',
      apps: [
        app({
          api_key_set: false,
          problem: '[servarr.apps.radarr] url is plain HTTP to arr.example.com',
          check: null,
        }),
      ],
    });
    const q = within(card);
    expect(q.getByText('Setup incomplete')).toBeTruthy();
    expect(q.getByText('Not set')).toBeTruthy();
    expect(q.getByText(/plain HTTP to arr\.example\.com/)).toBeTruthy();
    expect(q.getByText('Not checked')).toBeTruthy();
  });

  it('shows a dropped table as incomplete, with no URL or key row', async () => {
    const card = await mount({
      status: 'incomplete',
      apps: [
        app({
          app: 'lidarr',
          url: '',
          api_key_set: null,
          incomplete: true,
          problem: '[servarr.apps.lidarr] has no url or no api_key, so the skill ignores it',
          check: null,
        }),
      ],
    });
    const lidarr = within(within(card).getByTestId('servarr-app-lidarr'));
    expect(lidarr.getByText(/has no url or no api_key/)).toBeTruthy();
    expect(lidarr.queryByText('API key')).toBeNull();
  });

  it('shows the error reason and remedy of a failing app', async () => {
    const card = await mount({
      apps: [
        app({
          check: check({
            status: 'warn',
            detail: 'radarr at http://10.0.0.5:7878: connection_failed',
            remedy: 'radarr may be down, or a firewall is dropping the connection.',
          }),
        }),
      ],
    });
    const q = within(card);
    expect(q.getByText('Warning')).toBeTruthy();
    expect(q.getByText(/connection_failed/)).toBeTruthy();
    expect(q.getByTestId('servarr-remedy-radarr').textContent).toContain('firewall');
    expect(q.getByText('None of the apps passed the last check')).toBeTruthy();
  });

  it('says when the daemon report is old', async () => {
    const card = await mount({ apps: [app({ check: check({ stale: true }) })] });
    expect(within(card).getByText(/the daemon's last report is old/)).toBeTruthy();
  });

  it('does not link a URL that is not http(s)', async () => {
    const card = await mount({ apps: [app({ url: 'ftp://10.0.0.5', check: null })] });
    const q = within(card);
    expect(q.getByText('ftp://10.0.0.5')).toBeTruthy();
    expect(q.queryByRole('link', { name: 'ftp://10.0.0.5' })).toBeNull();
  });

  it('links Configure to the servarr section', async () => {
    const card = await mount();
    const configure = within(card).getByRole('link', { name: 'Configure' });
    expect(configure.getAttribute('href')).toBe('/istota/admin/config?section=servarr');
  });

  it('offers to add apps on an install with none, with no Check now or Edit', async () => {
    const card = await mount({
      status: 'disabled',
      apps: [],
      missing: ['sonarr', 'radarr', 'lidarr', 'prowlarr'],
      requesters: [],
      defaults: {},
    });
    const q = within(card);
    expect(q.getByText('Disabled')).toBeTruthy();
    expect(q.getByText(/No arr apps are connected/)).toBeTruthy();
    expect(q.getByRole('button', { name: 'Add apps' })).toBeTruthy();
    expect(q.queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(q.queryByRole('button', { name: 'Check now' })).toBeNull();
    expect(q.getByRole('link', { name: 'Configure' })).toBeTruthy();
    expect(q.getByRole('link', { name: 'Setup guide' }).getAttribute('target')).toBe('_blank');
  });
});

describe('Check now', () => {
  it('asks for a shallow run and reloads once it is answered', async () => {
    let finish: (v: unknown) => void = () => {};
    doctorRun.waitForDoctorRun.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const card = await mount();
    const button = within(card).getByRole('button', { name: 'Check now' });
    await fireEvent.click(button);
    expect(doctorRun.waitForDoctorRun).toHaveBeenCalledWith(false);

    api.getAdminConnections.mockResolvedValue({
      connections: [connection({ apps: [app({ check: check({ detail: 'after' }) })] })],
    });
    finish({ answered: true, deep: false, report: null });
    await waitFor(() => expect(screen.getByText('after')).toBeTruthy());
    expect(api.getAdminConnections).toHaveBeenCalledTimes(2);
  });

  it('keeps the last result and says so when the daemon does not answer', async () => {
    doctorRun.waitForDoctorRun.mockResolvedValue({ answered: false, deep: false, report: null });
    const card = await mount();
    await fireEvent.click(within(card).getByRole('button', { name: 'Check now' }));
    await waitFor(() =>
      expect(within(card).getByText(/has not answered the request yet/)).toBeTruthy(),
    );
    expect(within(card).getByText('radarr 5.9.1.9070 at http://10.0.0.5:7878')).toBeTruthy();
    expect(api.getAdminConnections).toHaveBeenCalledTimes(1);
  });

  it('reports a failed request as a notice', async () => {
    doctorRun.waitForDoctorRun.mockRejectedValue(new Error('API error: 403'));
    const card = await mount();
    await fireEvent.click(within(card).getByRole('button', { name: 'Check now' }));
    await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('API error: 403'));
  });
});

describe('editing', () => {
  const KEY_FILE = 'istota_servarr_radarr_api_key';

  /** The view's secrets entries for the four keys, as `GET /admin/config` lists them. */
  function keyFiles(readOnlyReason: string | null = null) {
    return ['sonarr', 'radarr', 'lidarr', 'prowlarr'].map((name) =>
      configSecret({
        name: `istota_servarr_${name}_api_key`,
        config_key: `servarr.apps.${name}.api_key`,
        set: name === 'radarr',
        source: name === 'radarr' ? 'file' : 'none',
        used_by: 'Servarr',
        read_only_reason: readOnlyReason,
      }),
    );
  }
  const UNMANAGED = { managed: false, source: null, at: null };
  const MANAGED = {
    config: { managed: true, source: 'ansible', at: null },
    secrets: UNMANAGED,
    admins: UNMANAGED,
  };

  function servarrView(over: Partial<AdminConfigView> = {}): AdminConfigView {
    return configView(
      [
        configField({
          path: ['servarr', 'private_hosts'],
          value: ['10.0.0.5'],
          type: 'list[str]',
          editor: 'list',
          in_file: true,
        }),
      ],
      { secrets: keyFiles(), ...over },
    );
  }

  const DISABLED: Partial<AdminServarrConnection> = {
    status: 'disabled',
    apps: [],
    missing: ['sonarr', 'radarr', 'lidarr', 'prowlarr'],
    requesters: [],
    defaults: {},
  };

  async function openEditor(
    view: AdminConfigView = servarrView(),
    over: Partial<AdminServarrConnection> = {},
    button = 'Edit',
  ) {
    api.getAdminConfig.mockResolvedValue(view);
    api.getAdminUsers.mockResolvedValue({
      users: [
        {
          user_id: 'alice',
          display_name: 'Alice',
          is_admin: true,
          state: 'password_set',
          identity: null,
        },
        {
          user_id: 'bob',
          display_name: 'Bob',
          is_admin: false,
          state: 'password_set',
          identity: null,
        },
      ],
      orphans: [],
      email_enabled: true,
    });
    const card = await mount(over);
    await fireEvent.click(within(card).getByRole('button', { name: button }));
    return card;
  }

  async function form(card: HTMLElement, app = 'radarr') {
    return within(await within(card).findByTestId(`servarr-edit-${app}`));
  }

  async function typeUrl(card: HTMLElement, url: string) {
    const radarr = await form(card);
    await fireEvent.input(radarr.getByLabelText(/^URL/), { target: { value: url } });
    return radarr;
  }

  function review(card: HTMLElement) {
    return fireEvent.click(within(card).getByRole('button', { name: 'Review changes' }));
  }

  beforeEach(() => {
    api.getAdminConfig.mockReset();
    api.getAdminUsers.mockReset();
    api.previewConfigChange.mockReset();
    api.submitConfigChange.mockReset();
    api.getConfigRequest.mockReset();
    api.startStepUp.mockReset();
  });

  it('starts the key input empty and never holds the stored value', async () => {
    const card = await openEditor();
    const radarr = await form(card);
    expect(radarr.getByText('set')).toBeTruthy();
    expect(radarr.queryByLabelText(/^New API key/)).toBeNull();
    await fireEvent.click(radarr.getByRole('button', { name: 'Replace key' }));
    const input = radarr.getByLabelText<HTMLInputElement>(/^New API key/);
    expect(input.value).toBe('');
    expect(input.type).toBe('password');
    expect(radarr.getByLabelText<HTMLInputElement>(/^URL/).value).toBe('http://10.0.0.5:7878');
  });

  it('sends an untouched key as no credential, and the change to the shared review', async () => {
    api.previewConfigChange.mockResolvedValue(
      configPreview({ diff: ['+url = "http://10.0.0.5:7879"'], changed_sections: ['servarr'] }),
    );
    const card = await openEditor();
    await typeUrl(card, 'http://10.0.0.5:7879');
    await review(card);
    expect(await screen.findByText('+url = "http://10.0.0.5:7879"')).toBeTruthy();
    expect(api.previewConfigChange).toHaveBeenCalledWith({
      base_digest: 'digest-1',
      ops: [
        { op: 'set', path: ['servarr', 'apps', 'radarr', 'url'], value: 'http://10.0.0.5:7879' },
      ],
      raw: null,
      secrets: {},
    });
  });

  it('sends a replaced key as one credential value and in no set op', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview({ secret_names: [KEY_FILE] }));
    const card = await openEditor();
    const radarr = await form(card);
    await fireEvent.click(radarr.getByRole('button', { name: 'Replace key' }));
    await fireEvent.input(radarr.getByLabelText(/^New API key/), {
      target: { value: 'fixture-arr-key-9' },
    });
    await review(card);
    await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalled());
    const changeSet = api.previewConfigChange.mock.calls[0][0];
    expect(changeSet.secrets).toEqual({ [KEY_FILE]: 'fixture-arr-key-9' });
    expect(changeSet.ops).toEqual([]);
  });

  it('removes the table of an app turned off and clears its key file', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview());
    const card = await openEditor();
    const radarr = await form(card);
    await fireEvent.click(radarr.getByLabelText('Enabled'));
    await review(card);
    await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalled());
    const changeSet = api.previewConfigChange.mock.calls[0][0];
    expect(changeSet.ops).toEqual([{ op: 'unset', path: ['servarr', 'apps', 'radarr'] }]);
    expect(changeSet.secrets).toEqual({ [KEY_FILE]: '' });
  });

  it('shows a read-only key file with its reason and no way to replace it', async () => {
    const reason = 'Managed by ansible. Change it there.';
    const card = await openEditor(servarrView({ secrets: keyFiles(reason) }));
    const radarr = await form(card);
    expect(radarr.getByText(reason)).toBeTruthy();
    expect(radarr.queryByRole('button', { name: 'Replace key' })).toBeNull();
  });

  it('refuses plain HTTP to a host not in the private hosts before any preview', async () => {
    const card = await openEditor();
    const radarr = await typeUrl(card, 'http://arr.example.com:7878');
    await review(card);
    expect(await radarr.findByText(/not in the private hosts/)).toBeTruthy();
    expect(api.previewConfigChange).not.toHaveBeenCalled();
  });

  it('applies through step-up and follows the request', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview());
    api.startStepUp.mockResolvedValue({
      request_id: 'step-1',
      method: 'email' as const,
      email_hint: 'a•••@example.com',
      expires_at: 'later',
    });
    api.submitConfigChange.mockResolvedValue({ id: 17 });
    api.getConfigRequest.mockResolvedValue(configRequest({ id: 17, status: 'pending' }));
    const card = await openEditor();
    await typeUrl(card, 'http://10.0.0.5:7879');
    await review(card);
    await fireEvent.click(await screen.findByRole('button', { name: 'Apply…' }));
    expect(api.startStepUp).toHaveBeenCalledWith('admin_config_change', undefined);
    await screen.findByText(/a•••@example.com/);
    await fireEvent.input(screen.getByLabelText('Confirmation code'), {
      target: { value: '123456' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(await within(card).findByText('Change #17')).toBeTruthy();
    expect(api.submitConfigChange.mock.calls[0][0].step_up).toEqual({
      request_id: 'step-1',
      code: '123456',
    });
    expect(within(card).queryByTestId('servarr-edit-radarr')).toBeNull();
  });

  it('is read-only with the reason when there is no config applier', async () => {
    const reason = 'This install has no config applier; changes are made on the host.';
    const card = await openEditor(
      servarrView({ editable: false, applier: { kind: 'none', available: false, reason } }),
    );
    expect(await within(card).findByText(reason)).toBeTruthy();
    expect(within(card).queryByTestId('servarr-edit-radarr')).toBeNull();
  });

  it('is read-only and names the plan when config.toml is managed', async () => {
    const card = await openEditor(servarrView({ managed: MANAGED }));
    expect(await within(card).findByText(/managed by ansible/)).toBeTruthy();
    expect(within(card).queryByTestId('servarr-edit-radarr')).toBeNull();
  });

  it('keeps the edits when config.toml changed under the card', async () => {
    api.previewConfigChange
      .mockRejectedValueOnce(
        new api.ConfigChangeError('changed', 409, 'config_changed', { digest: 'digest-2' }),
      )
      .mockResolvedValue(configPreview());
    const card = await openEditor();
    await typeUrl(card, 'http://10.0.0.5:7879');
    api.getAdminConfig.mockResolvedValue(servarrView({ document_digest: 'digest-2' }));
    api.getAdminConnections.mockResolvedValue({
      connections: [connection({ requesters: ['bob', 'alice'] })],
    });
    await review(card);
    expect(await within(card).findByText(/changed on the host/)).toBeTruthy();
    const kept = await form(card);
    expect(kept.getByLabelText<HTMLInputElement>(/^URL/).value).toBe('http://10.0.0.5:7879');
    await review(card);
    await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalledTimes(2));
    expect(api.previewConfigChange.mock.calls[1][0]).toMatchObject({
      base_digest: 'digest-2',
      ops: [
        { op: 'set', path: ['servarr', 'apps', 'radarr', 'url'], value: 'http://10.0.0.5:7879' },
      ],
    });
  });

  it('drops the edits and goes read-only when the changed file is now managed', async () => {
    api.previewConfigChange.mockRejectedValueOnce(
      new api.ConfigChangeError('changed', 409, 'config_changed', { digest: 'digest-2' }),
    );
    const card = await openEditor();
    await typeUrl(card, 'http://10.0.0.5:7879');
    api.getAdminConfig.mockResolvedValue(
      servarrView({ document_digest: 'digest-2', managed: MANAGED }),
    );
    await review(card);
    expect(await within(card).findByText(/managed by ansible/)).toBeTruthy();
    expect(within(card).queryByTestId('servarr-edit-radarr')).toBeNull();
  });

  it('opens the form on an install with no apps (ISSUE-767)', async () => {
    const card = await openEditor(servarrView(), DISABLED, 'Add apps');
    const sonarr = await form(card, 'sonarr');
    expect(sonarr.getByLabelText('Enabled')).toBeTruthy();
    expect(within(card).queryByText(/no arr apps/i)).toBeNull();
  });

  it('adds a first app as its URL and a key file, from an empty card', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview());
    const card = await openEditor(servarrView(), DISABLED, 'Add apps');
    const sonarr = await form(card, 'sonarr');
    await fireEvent.click(sonarr.getByLabelText('Enabled'));
    const url = sonarr.getByLabelText<HTMLInputElement>(/^URL/);
    expect(url.placeholder).toBe('http://192.168.1.10:8989');
    await fireEvent.input(url, { target: { value: 'https://sonarr.example.com' } });
    await fireEvent.input(sonarr.getByLabelText(/^API key/), {
      target: { value: 'fixture-arr-key-1' },
    });
    await review(card);
    await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalled());
    const changeSet = api.previewConfigChange.mock.calls[0][0];
    expect(changeSet.ops).toEqual([
      {
        op: 'set',
        path: ['servarr', 'apps', 'sonarr', 'url'],
        value: 'https://sonarr.example.com',
      },
    ]);
    expect(changeSet.secrets).toEqual({ istota_servarr_sonarr_api_key: 'fixture-arr-key-1' });
  });

  it('offers to add a plain-HTTP host to the private hosts before review', async () => {
    api.previewConfigChange.mockResolvedValue(configPreview());
    const card = await openEditor();
    const radarr = await typeUrl(card, 'http://10.0.0.9:7878');
    expect(radarr.getByText(/not in the private hosts/)).toBeTruthy();
    await fireEvent.click(radarr.getByRole('button', { name: 'Add 10.0.0.9 to private hosts' }));
    expect(radarr.queryByText(/not in the private hosts/)).toBeNull();
    await review(card);
    await waitFor(() => expect(api.previewConfigChange).toHaveBeenCalled());
    expect(api.previewConfigChange.mock.calls[0][0].ops).toContainEqual({
      op: 'set',
      path: ['servarr', 'private_hosts'],
      value: ['10.0.0.5', '10.0.0.9'],
    });
  });

  it('clears the refused URL once the quick fix answers it', async () => {
    const card = await openEditor();
    const radarr = await typeUrl(card, 'http://10.0.0.9:7878');
    await review(card);
    expect(api.previewConfigChange).not.toHaveBeenCalled();
    await fireEvent.click(radarr.getByRole('button', { name: 'Add 10.0.0.9 to private hosts' }));
    expect(radarr.queryByText(/not in the private hosts/)).toBeNull();
  });

  it('keeps Add defaults open while its last value is cleared', async () => {
    const card = await openEditor(servarrView(), DISABLED, 'Add apps');
    const sonarr = await form(card, 'sonarr');
    await fireEvent.click(sonarr.getByLabelText('Enabled'));
    const details = sonarr.getByText('Add defaults').closest('details')!;
    details.open = true;
    const profile = sonarr.getByLabelText(/^Quality profile/);
    await fireEvent.input(profile, { target: { value: 'x' } });
    await fireEvent.input(profile, { target: { value: '' } });
    expect(details.open).toBe(true);
  });

  it('withdraws Add apps and says why when the card cannot change [servarr]', async () => {
    const reason = 'This install has no config applier; changes are made on the host.';
    const card = await openEditor(
      servarrView({ editable: false, applier: { kind: 'none', available: false, reason } }),
      DISABLED,
      'Add apps',
    );
    expect(await within(card).findByText(reason)).toBeTruthy();
    expect(within(card).queryByRole('button', { name: 'Add apps' })).toBeNull();
    expect(within(card).queryByTestId('servarr-edit-sonarr')).toBeNull();
  });
});
