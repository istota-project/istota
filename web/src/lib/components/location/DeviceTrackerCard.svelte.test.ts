import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import DeviceTrackerCard from './DeviceTrackerCard.svelte';
import { encodeProvisioning } from '$lib/location/provisioning';
import type { TrackerStatus } from '$lib/platform/nativeLocation';

const PLAIN_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1';

function setUserAgent(ua: string): void {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}

const STATUS: TrackerStatus = {
  tracking: true,
  profile: 'detailed',
  authorization: 'always',
  queuedPoints: 12,
  lastSentAt: new Date().toISOString(),
  lastError: null,
  droppedPoints: 0,
  configured: true,
  endpointHost: 'example.invalid',
  deviceId: 'ABC-123',
};

function installShell(status: Partial<TrackerStatus> = {}, scanner = true) {
  setUserAgent(`${PLAIN_SAFARI} IstotaApp/0.7.0`);
  const merged = { ...STATUS, ...status };
  const tracker = {
    configure: vi.fn().mockResolvedValue(undefined),
    start: vi.fn().mockResolvedValue(merged),
    stop: vi.fn().mockResolvedValue({ ...merged, tracking: false }),
    status: vi.fn().mockResolvedValue(merged),
    sendNow: vi.fn().mockResolvedValue({ sent: 12 }),
    requestPermissions: vi.fn().mockResolvedValue({ authorization: 'always' }),
    openAppSettings: vi.fn().mockResolvedValue(undefined),
  };
  const qr = { scan: vi.fn().mockResolvedValue({ value: null }) };
  (globalThis as Record<string, unknown>).Capacitor = {
    Plugins: scanner
      ? { IstotaLocation: tracker, IstotaQrScanner: qr }
      : { IstotaLocation: tracker },
  };
  return { tracker, qr };
}

/** Let the mount effect's awaits settle before asserting on the readout. */
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  setUserAgent(PLAIN_SAFARI);
  delete (globalThis as Record<string, unknown>).Capacitor;
});

afterEach(async () => {
  cleanup();
  vi.restoreAllMocks();
  await new Promise((resolve) => setTimeout(resolve, 0));
});

describe('in a browser', () => {
  it('says tracking is per-device instead of rendering nothing', async () => {
    // Rendering nothing would leave someone who set this up on their phone
    // unable to tell whether the section is missing by design.
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText(/set up per device/i)).toBeTruthy();
  });

  it('offers no controls at all', async () => {
    render(DeviceTrackerCard);
    await settle();
    expect(screen.queryByRole('button')).toBeNull();
  });
});

describe('in the shell', () => {
  it('shows the readout that says tracking is still alive', async () => {
    installShell();
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('12')).toBeTruthy();
    expect(screen.getByText('example.invalid')).toBeTruthy();
    expect(screen.getByText('On')).toBeTruthy();
  });

  it('offers Start rather than Stop when tracking is off', async () => {
    installShell({ tracking: false });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('Start')).toBeTruthy();
    expect(screen.queryByText('Stop')).toBeNull();
  });

  it('sends the queue on demand and says how many landed', async () => {
    const { tracker } = installShell();
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Send now'));
    await settle();
    expect(tracker.sendNow).toHaveBeenCalled();
    expect(screen.getByText(/sent 12 points/i)).toBeTruthy();
  });
});

describe('permission states', () => {
  it('routes a denied authorization to iOS Settings, the only way back', async () => {
    const { tracker } = installShell({ authorization: 'denied', tracking: false });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Open iOS Settings'));
    await settle();
    expect(tracker.openAppSettings).toHaveBeenCalled();
    // Offering Start here would arm a tracker that logs nothing.
    expect(screen.queryByText('Start')).toBeNull();
  });

  it('asks again for Always when only When In Use was granted', async () => {
    // iOS refuses the Always prompt as a first ask, so this is the second tap.
    const { tracker } = installShell({ authorization: 'whenInUse' });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText(/nothing is logged in the background/i)).toBeTruthy();
    await fireEvent.click(screen.getByText('Allow always'));
    await settle();
    expect(tracker.requestPermissions).toHaveBeenCalled();
  });
});

describe('provisioning', () => {
  it('offers a scan when the device has no token yet', async () => {
    installShell({ configured: false });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('Scan provisioning code')).toBeTruthy();
    expect(screen.queryByText('Send now')).toBeNull();
  });

  it('configures the tracker from a scanned code', async () => {
    const { tracker, qr } = installShell({ configured: false });
    const provisioning = { endpoint: 'https://example.invalid/webhooks/location', token: 'tok' };
    qr.scan.mockResolvedValue({ value: encodeProvisioning(provisioning) });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Scan provisioning code'));
    await settle();
    expect(tracker.configure).toHaveBeenCalledWith(provisioning);
  });

  it('says so when the code is not one of ours', async () => {
    const { tracker, qr } = installShell({ configured: false });
    qr.scan.mockResolvedValue({ value: 'https://example.com/an-unrelated-qr' });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Scan provisioning code'));
    await settle();
    expect(tracker.configure).not.toHaveBeenCalled();
    expect(screen.getByText(/not an istota provisioning code/i)).toBeTruthy();
  });

  it('stays quiet when the user backs out of the scanner', async () => {
    // A cancel is a decision, not a failure.
    const { qr } = installShell({ configured: false });
    qr.scan.mockResolvedValue({ value: null });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Scan provisioning code'));
    await settle();
    expect(screen.queryByText(/not an istota provisioning code/i)).toBeNull();
  });

  it('tells an older shell to update instead of showing a dead Scan button', async () => {
    installShell({ configured: false }, false);
    render(DeviceTrackerCard);
    await settle();
    expect(screen.queryByText('Scan provisioning code')).toBeNull();
    expect(screen.getByText(/update it from testflight/i)).toBeTruthy();
  });
});
