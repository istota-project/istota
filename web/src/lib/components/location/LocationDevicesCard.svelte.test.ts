import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import type { LocationDevice } from '$lib/api';
import { formatDate } from '@minot/ui/dateFormat';
import { clearNotices, currentNotice } from '@minot/ui/stores/notices';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);

const { default: LocationDevicesCard } = await import('./LocationDevicesCard.svelte');

const PLAIN_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1';

const PHONE_ID = '6F1C2B3A-1111-4222-8333-444455556666';

const APP_ROW: LocationDevice = {
  id: 3,
  kind: 'app',
  device_id: PHONE_ID,
  label: 'iPhone',
  token_tail: 'x9Qa',
  created_at: '2026-10-01T09:00:00.000Z',
  last_used_at: new Date(Date.now() - 5 * 60_000).toISOString(),
};

const CODE_ROW: LocationDevice = {
  id: 2,
  kind: 'code',
  device_id: null,
  label: 'QR code',
  token_tail: 'Zz01',
  created_at: '2026-09-20T09:00:00.000Z',
  last_used_at: null,
};

function setUserAgent(ua: string): void {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}

/** The iOS shell with a tracker reporting `deviceId`. */
function installShell(deviceId: string) {
  setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.7.0`);
  (globalThis as Record<string, unknown>).Capacitor = {
    Plugins: {
      IstotaLocation: {
        status: vi.fn().mockResolvedValue({
          tracking: true,
          profile: 'detailed',
          authorization: 'always',
          queuedPoints: 0,
          lastSentAt: null,
          lastError: null,
          droppedPoints: 0,
          configured: true,
          endpointHost: 'example.invalid',
          deviceId,
        }),
      },
    },
  };
}

function words(el: Element | null): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim();
}

function row(text: string): HTMLElement {
  const item = screen.getByText(text, { exact: false }).closest('li');
  if (!item) throw new Error(`no row for ${text}`);
  return item as HTMLElement;
}

beforeEach(() => {
  setUserAgent(PLAIN_SAFARI);
  delete (globalThis as Record<string, unknown>).Capacitor;
  clearNotices();
  api.listLocationDevices.mockResolvedValue({ devices: [APP_ROW, CODE_ROW], legacy_token: false });
  api.deleteLocationDevice.mockResolvedValue({ ok: true });
  api.deleteLegacyIngestToken.mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('LocationDevicesCard', () => {
  it('lists each device with its label, tail, added date and last send', async () => {
    render(LocationDevicesCard);
    await waitFor(() => expect(screen.getByText('iPhone · …x9Qa')).toBeTruthy());

    const app = row('iPhone · …x9Qa');
    expect(words(app)).toContain(`Added ${formatDate(APP_ROW.created_at)} · last sent 5m ago`);
    const code = row('QR code · …Zz01');
    expect(words(code)).toContain(`Added ${formatDate(CODE_ROW.created_at)} · last sent never`);
    // Not in the shell, so no row can be this phone.
    expect(screen.queryByText('This phone')).toBeNull();
  });

  it('marks the row whose device id is the tracker on this phone', async () => {
    installShell(PHONE_ID);
    render(LocationDevicesCard);
    await waitFor(() => expect(screen.getByText('This phone')).toBeTruthy());
    expect(words(row('iPhone · …x9Qa'))).toContain('This phone');
    expect(words(row('QR code · …Zz01'))).not.toContain('This phone');
  });

  it('shows the empty state when nothing can send', async () => {
    api.listLocationDevices.mockResolvedValue({ devices: [], legacy_token: false });
    render(LocationDevicesCard);
    await waitFor(() =>
      expect(screen.getByText('No devices can send locations yet.')).toBeTruthy(),
    );
  });

  it('removes a device after confirming and reloads the list', async () => {
    render(LocationDevicesCard);
    await waitFor(() => expect(screen.getByText('iPhone · …x9Qa')).toBeTruthy());
    api.listLocationDevices.mockResolvedValue({ devices: [CODE_ROW], legacy_token: false });

    await fireEvent.click(screen.getByRole('button', { name: 'Remove iPhone ending x9Qa' }));
    const dialog = screen.getByRole('dialog', { name: 'Remove device' });
    expect(words(dialog)).toContain('iPhone stops sending locations until it is set up again.');
    expect(api.deleteLocationDevice).not.toHaveBeenCalled();

    await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(screen.queryByText('iPhone · …x9Qa')).toBeNull());
    expect(api.deleteLocationDevice).toHaveBeenCalledWith(3);
    expect(api.listLocationDevices).toHaveBeenCalledTimes(2);
    expect(screen.getByText('QR code · …Zz01')).toBeTruthy();
  });

  it('lists the shared token and removes it with its own confirmation', async () => {
    api.listLocationDevices.mockResolvedValue({ devices: [], legacy_token: true });
    render(LocationDevicesCard);
    await waitFor(() =>
      expect(screen.getByText('Shared token (from before per-device tokens)')).toBeTruthy(),
    );
    expect(words(row('Shared token'))).toContain(
      'Every device set up before this change uses it. Remove it once each one has been set up again.',
    );
    expect(screen.queryByText('No devices can send locations yet.')).toBeNull();

    api.listLocationDevices.mockResolvedValue({ devices: [], legacy_token: false });
    await fireEvent.click(screen.getByRole('button', { name: 'Remove the shared token' }));
    const dialog = screen.getByRole('dialog', { name: 'Remove the shared token' });
    expect(words(dialog)).toContain(
      'Every device still using the shared token stops sending locations.',
    );
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
    await waitFor(() =>
      expect(screen.getByText('No devices can send locations yet.')).toBeTruthy(),
    );
    expect(api.deleteLegacyIngestToken).toHaveBeenCalledTimes(1);
    expect(api.deleteLocationDevice).not.toHaveBeenCalled();
  });

  it('reports a refused removal through the notice drawer', async () => {
    api.deleteLocationDevice.mockRejectedValue(new Error('device not found'));
    render(LocationDevicesCard);
    await waitFor(() => expect(screen.getByText('iPhone · …x9Qa')).toBeTruthy());

    await fireEvent.click(screen.getByRole('button', { name: 'Remove iPhone ending x9Qa' }));
    const dialog = screen.getByRole('dialog', { name: 'Remove device' });
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(get(currentNotice)?.message).toBe('device not found'));
    expect(get(currentNotice)?.severity).toBe('error');
  });
});
