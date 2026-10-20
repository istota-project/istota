import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, fireEvent, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

import type { AdminNextcloudCheck, AdminServarrApp, AdminServarrConnection } from '$lib/api';

/**
 * The servarr card on `/admin/connections`: status only.
 *
 * It shows what the config names per arr app and what the daemon's last
 * `servarr.connection.<app>` check found, and offers a fresh doctor run. The
 * payload never carries a key, so the card can only say whether one is set.
 * `[servarr]` is edited in Configuration, which Configure and Add apps open.
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
    expect(
      radarr.getByText(
        (_, el) => el?.tagName === 'P' && /checked 12m ago/.test(el.textContent ?? ''),
      ),
    ).toBeTruthy();
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

  it('links Configure to the servarr section and has no form of its own', async () => {
    const card = await mount();
    const configure = within(card).getByRole('link', { name: 'Configure' });
    expect(configure.getAttribute('href')).toBe('/istota/admin/config?section=servarr');
    expect(within(card).queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(within(card).queryByRole('textbox')).toBeNull();
  });

  it('links Add apps to Configuration on an install with none, with no Check now', async () => {
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
    expect(q.getByRole('link', { name: 'Add apps' }).getAttribute('href')).toBe(
      '/istota/admin/config?section=servarr',
    );
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
    expect(doctorRun.waitForDoctorRun).toHaveBeenCalledWith(false, {
      signal: expect.any(AbortSignal),
    });

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

  it('ends the wait when the page goes, and reloads nothing', async () => {
    let finish!: (v: unknown) => void;
    doctorRun.waitForDoctorRun.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const card = await mount();
    await fireEvent.click(within(card).getByRole('button', { name: 'Check now' }));
    const { signal } = doctorRun.waitForDoctorRun.mock.calls[0][1];
    expect(signal.aborted).toBe(false);
    cleanup();
    expect(signal.aborted).toBe(true);
    // `answered` as well, so a card that ignored `cancelled` would reload.
    finish({ answered: true, cancelled: true, deep: false, report: null });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(api.getAdminConnections).toHaveBeenCalledTimes(1);
    expect(notices.notifyError).not.toHaveBeenCalled();
  });
});
