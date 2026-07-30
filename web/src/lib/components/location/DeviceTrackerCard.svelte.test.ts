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

  it('keeps Stop reachable when the permission was downgraded mid-run', async () => {
    // The permission prompt used to *replace* the toggle, so a user whose
    // authorization dropped to While Using could not stop the tracker at all.
    const { tracker } = installShell({ authorization: 'whenInUse', tracking: true });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('Allow always')).toBeTruthy();
    await fireEvent.click(screen.getByText('Stop'));
    await settle();
    expect(tracker.stop).toHaveBeenCalled();
  });

  it('offers Start alongside the prompt when permission was never asked for', async () => {
    installShell({ authorization: 'notDetermined', tracking: false });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('Allow location')).toBeTruthy();
    expect(screen.getByText('Start')).toBeTruthy();
  });
});

describe('a pause is not a stall', () => {
  it('says paused, and says why', async () => {
    // An empty queue and an ageing last-sent time is what a dead tracker looks
    // like. Without this the card reads "On" and sends someone hunting a fault
    // in the battery saving working correctly.
    installShell({ tracking: true, paused: true });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('Paused')).toBeTruthy();
    expect(screen.getByText(/has not moved for a while/i)).toBeTruthy();
  });

  it('keeps Stop available while paused', async () => {
    // Tracking is still armed, so the control that turns it off must stay.
    const { tracker } = installShell({ tracking: true, paused: true });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Stop'));
    await settle();
    expect(tracker.stop).toHaveBeenCalled();
  });

  it('lets a permission problem outrank it', async () => {
    // The pause resolves itself; the permission is why nothing gets logged at
    // all. Two lines would put the reassuring one beside the alarming one.
    installShell({ tracking: true, paused: true, authorization: 'whenInUse' });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText(/nothing is logged in the background/i)).toBeTruthy();
    expect(screen.queryByText(/has not moved for a while/i)).toBeNull();
  });

  it('reads as On against a shell too old to report a pause', async () => {
    // `paused` is absent rather than false there, and absent is exactly the
    // behaviour that shell has — so no version gate, just a falsy read.
    const status = { ...STATUS, tracking: true };
    delete (status as { paused?: boolean }).paused;
    installShell(status);
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('On')).toBeTruthy();
    expect(screen.queryByText('Paused')).toBeNull();
  });

  it('says Off, not Paused, when tracking is stopped', async () => {
    installShell({ tracking: false, paused: true });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('Off')).toBeTruthy();
    expect(screen.queryByText('Paused')).toBeNull();
  });
});

describe('the caption is not a control', () => {
  it('clicking the word "Tracking" does not stop the tracker', async () => {
    // SettingsField wraps its label text and its slot in one <label>, and a
    // <button> is a labelable element — so without `labelled={false}` the
    // caption becomes the Stop button's trigger and stops background
    // location tracking on a click that looks inert.
    const { tracker } = installShell({ tracking: true });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Tracking'));
    await settle();
    expect(tracker.stop).not.toHaveBeenCalled();
  });

  it('clicking the word "Profile" does not change the profile', async () => {
    const { tracker } = installShell({ tracking: true });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Profile'));
    await settle();
    expect(tracker.start).not.toHaveBeenCalled();
  });
});

/**
 * The bits-ui Select cannot be opened under jsdom — it needs pointer-capture
 * APIs jsdom does not implement, and stubbing them is not enough to make the
 * popover mount. So the switch itself is not driven here. What is asserted is
 * everything the DOM does expose about the policy behind it: that a stopped
 * tracker says the choice is deferred, and that Start carries a profile.
 */
describe('the profile picker', () => {
  it('says the choice is deferred while tracking is off', async () => {
    // `start` is the plugin's only way to set a profile and it also arms the
    // tracker, so applying the choice on selection would begin recording for
    // someone who had deliberately stopped. The card holds it instead.
    installShell({ tracking: false, profile: 'detailed' });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText(/applies when you start tracking/i)).toBeTruthy();
  });

  it('says nothing of the sort while tracking, where a switch is immediate', async () => {
    installShell({ tracking: true, profile: 'detailed' });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.queryByText(/applies when you start tracking/i)).toBeNull();
  });

  it('carries a profile into Start rather than leaving it to the plugin default', async () => {
    const { tracker } = installShell({ tracking: false, profile: 'places' });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Start'));
    await settle();
    expect(tracker.start).toHaveBeenCalledWith({ profile: 'places' });
  });
});

describe('failure handling', () => {
  it('shows a refusal and falls back to what the device actually has', async () => {
    // A rejected action leaves the tracker as it was, so the readout has to go
    // back to the device rather than keep showing what was asked for.
    const { tracker } = installShell({ tracking: true });
    tracker.stop.mockRejectedValue(new Error('location authorization is required'));
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Stop'));
    await settle();
    expect(screen.getByText(/location authorization is required/i)).toBeTruthy();
    // Once on mount, once re-reading the device after the refusal.
    expect(tracker.status).toHaveBeenCalledTimes(2);
  });

  it('surfaces a dropped-point count and the last error, which are the alarm', async () => {
    installShell({ droppedPoints: 3, lastError: 'The request timed out.' });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('Dropped')).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
    expect(screen.getByText('The request timed out.')).toBeTruthy();
  });

  it('hides both rows when there is nothing wrong', async () => {
    installShell();
    render(DeviceTrackerCard);
    await settle();
    expect(screen.queryByText('Dropped')).toBeNull();
    expect(screen.queryByText('Last error')).toBeNull();
  });

  it('does not let a stale success outlive the action it described', async () => {
    const { tracker } = installShell();
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Send now'));
    await settle();
    expect(screen.getByText(/sent 12 points/i)).toBeTruthy();
    await fireEvent.click(screen.getByText('Refresh'));
    await settle();
    expect(screen.queryByText(/sent 12 points/i)).toBeNull();
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
