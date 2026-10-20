import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, waitFor, fireEvent, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

import type { AdminNextcloudCheck, AdminNextcloudConnection } from '$lib/api';

/**
 * The Nextcloud card on `/admin/connections`.
 *
 * Read-only: it shows what the config names and what the daemon's last
 * `nextcloud.connection` check found, and offers one action, a fresh doctor
 * run. Every fixture here carries no WhatsApp entry, which is also the guard on
 * the page rendering each card from its own entry rather than from WhatsApp's.
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
    detail: 'signed in as istota (Istota) on Nextcloud 31.0.5',
    remedy: '',
    at: new Date(Date.now() - 12 * 60_000).toISOString(),
    stale: false,
    ...over,
  };
}

function connection(over: Partial<AdminNextcloudConnection> = {}): AdminNextcloudConnection {
  return {
    id: 'nextcloud',
    label: 'Nextcloud',
    configured: true,
    url: 'https://cloud.example.com',
    username: 'istota',
    talk_enabled: true,
    talk_bot_username: 'istota',
    storage: 'nextcloud',
    check: check(),
    ...over,
  };
}

async function mount(over: Partial<AdminNextcloudConnection> = {}) {
  api.getAdminConnections.mockResolvedValue({ connections: [connection(over)] });
  render(Page);
  return screen.findByTestId('nextcloud-card');
}

beforeEach(() => {
  api.getAdminConnections.mockReset();
  doctorRun.waitForDoctorRun.mockReset();
  notices.notifyError.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('the Nextcloud card', () => {
  it('shows the connection and a passing check', async () => {
    const card = await mount();
    const q = within(card);
    expect(q.getByText('OK')).toBeTruthy();
    const server = q.getByRole('link', { name: 'https://cloud.example.com' });
    expect(server.getAttribute('href')).toBe('https://cloud.example.com');
    expect(server.getAttribute('target')).toBe('_blank');
    expect(q.getByText('On, as istota')).toBeTruthy();
    expect(q.getByText('Nextcloud files, through the mount')).toBeTruthy();
    expect(q.getByText('signed in as istota (Istota) on Nextcloud 31.0.5')).toBeTruthy();
    expect(
      q.getByText((_, el) => el?.tagName === 'P' && /checked 12m ago/.test(el.textContent ?? '')),
    ).toBeTruthy();
    expect(q.queryByTestId('nextcloud-remedy')).toBeNull();
    expect(q.queryByText(/last report is old/)).toBeNull();
    const edit = q.getByRole('link', { name: 'Configure' });
    expect(edit.getAttribute('href')).toMatch(/\/admin\/config$/);
  });

  it('shows the remedy under a warning', async () => {
    const card = await mount({
      check: check({
        status: 'warn',
        detail: 'cloud.example.com answered HTTP 503',
        remedy: 'The server is failing, or Nextcloud is in maintenance mode.',
      }),
    });
    const q = within(card);
    expect(q.getByText('Warning')).toBeTruthy();
    expect(q.getByTestId('nextcloud-remedy').textContent).toContain('maintenance mode');
  });

  it('shows the remedy under a failure', async () => {
    const card = await mount({
      check: check({
        status: 'fail',
        detail: 'signed in as istota; [talk] bot_username is Istota',
        remedy: 'Set [talk] bot_username to the account id shown.',
      }),
    });
    const q = within(card);
    expect(q.getByText('Failed')).toBeTruthy();
    expect(q.getByTestId('nextcloud-remedy').textContent).toContain('bot_username');
  });

  it('says when the daemon report is old', async () => {
    const card = await mount({ check: check({ stale: true }) });
    expect(within(card).getByText(/the daemon's last report is old/)).toBeTruthy();
  });

  it('reads no result as not checked, and still offers a check', async () => {
    const card = await mount({ check: null });
    const q = within(card);
    expect(q.getByText('Not checked')).toBeTruthy();
    expect(q.getByRole('button', { name: 'Check now' })).toBeTruthy();
  });

  it('reads a skip as not checked and shows why', async () => {
    const card = await mount({
      check: check({
        status: 'skip',
        detail: 'sign-in cannot be observed without a network request (probe disabled)',
      }),
    });
    const q = within(card);
    expect(q.getByText('Not checked')).toBeTruthy();
    expect(q.getByText(/probe disabled/)).toBeTruthy();
  });

  it('shows Talk off and a local workspace', async () => {
    const card = await mount({ talk_enabled: false, storage: 'local' });
    const q = within(card);
    expect(q.getByText('Off')).toBeTruthy();
    expect(q.getByText('Local workspace')).toBeTruthy();
  });

  it('does not link a server address that is not http(s)', async () => {
    const card = await mount({ url: 'placeholder' });
    const q = within(card);
    expect(q.getByText('placeholder')).toBeTruthy();
    expect(q.queryByRole('link', { name: 'placeholder' })).toBeNull();
  });

  it('says an unconfigured install has no Nextcloud, with no button', async () => {
    const card = await mount({
      configured: false,
      url: '',
      username: '',
      storage: 'local',
      check: null,
    });
    const q = within(card);
    expect(q.getByText(/has no Nextcloud/)).toBeTruthy();
    expect(q.queryByRole('button', { name: 'Check now' })).toBeNull();
    expect(q.getByRole('link', { name: 'Configure' })).toBeTruthy();
  });
});

describe('Check now', () => {
  it('asks for a shallow run and reloads once it is answered', async () => {
    let finish: (v: unknown) => void = () => {};
    doctorRun.waitForDoctorRun.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const card = await mount({ check: check({ detail: 'before' }) });
    const button = within(card).getByRole('button', { name: 'Check now' });
    await fireEvent.click(button);
    expect(doctorRun.waitForDoctorRun).toHaveBeenCalledWith(false, {
      signal: expect.any(AbortSignal),
    });
    await waitFor(() => expect(button.hasAttribute('disabled')).toBe(true));

    api.getAdminConnections.mockResolvedValue({
      connections: [connection({ check: check({ detail: 'after' }) })],
    });
    finish({ answered: true, deep: false, report: null });
    await waitFor(() => expect(screen.getByText('after')).toBeTruthy());
    expect(api.getAdminConnections).toHaveBeenCalledTimes(2);
    expect(button.hasAttribute('disabled')).toBe(false);
  });

  it('keeps the last result and says so when the daemon does not answer', async () => {
    doctorRun.waitForDoctorRun.mockResolvedValue({ answered: false, deep: false, report: null });
    const card = await mount({ check: check({ detail: 'before' }) });
    await fireEvent.click(within(card).getByRole('button', { name: 'Check now' }));
    await waitFor(() =>
      expect(within(card).getByText(/has not answered the request yet/)).toBeTruthy(),
    );
    expect(within(card).getByText('before')).toBeTruthy();
    expect(api.getAdminConnections).toHaveBeenCalledTimes(1);
  });

  it('reports a failed request as a notice, not on the card', async () => {
    // A failed request is an action failing, so it goes to the notice drawer
    // the way the servarr card's does; the card keeps its last result.
    doctorRun.waitForDoctorRun.mockRejectedValue(new Error('API error: 403'));
    const card = await mount();
    await fireEvent.click(within(card).getByRole('button', { name: 'Check now' }));
    await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('API error: 403'));
    expect(within(card).queryByText(/API error: 403/)).toBeNull();
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

  it('raises no notice for a reload that fails after the page has gone', async () => {
    doctorRun.waitForDoctorRun.mockResolvedValue({
      answered: true,
      cancelled: false,
      deep: false,
      report: null,
    });
    const card = await mount();
    let failReload!: (e: Error) => void;
    api.getAdminConnections.mockImplementationOnce(
      () => new Promise((_, reject) => (failReload = reject)),
    );
    await fireEvent.click(within(card).getByRole('button', { name: 'Check now' }));
    await waitFor(() => expect(failReload).toBeTypeOf('function'));
    cleanup();
    failReload(new Error('API error: 502'));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(notices.notifyError).not.toHaveBeenCalled();
  });

  it('says the daemon has not answered as a status line', async () => {
    doctorRun.waitForDoctorRun.mockResolvedValue({ answered: false, deep: false, report: null });
    const card = await mount();
    await fireEvent.click(within(card).getByRole('button', { name: 'Check now' }));
    const line = await within(card).findByRole('status');
    expect(line.textContent).toMatch(/has not answered the request yet/);
  });
});
