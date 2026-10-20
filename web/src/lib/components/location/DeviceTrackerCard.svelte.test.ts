import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import DeviceTrackerCard from './DeviceTrackerCard.svelte';
import { formatDateTime } from '$lib/dateFormat';
import { encodeProvisioning } from '$lib/location/provisioning';
import type { TrackerStatus } from '$lib/platform/nativeLocation';
import type { Place } from '$lib/api';

/**
 * The card reads the user's places to fill the wifi-zone picker. Mocked
 * through `importOriginal` rather than a bare factory so anything else in the
 * component tree that reaches for `$lib/api` still gets the real module.
 */
const placesMock = vi.fn();
const createDeviceMock = vi.fn();
vi.mock('$lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api')>()),
  getLocationPlaces: () => placesMock(),
  createLocationDevice: (...args: unknown[]) => createDeviceMock(...args),
}));

const MINTED = {
  id: 7,
  token: 'minted-token',
  endpoint: 'https://example.invalid/webhooks/location',
  webhook_url: 'https://example.invalid/webhooks/location?token=minted-token',
};

const PLACES: Place[] = [
  { id: 1, name: 'Home', lat: 52.23, lon: 21.01, radius_meters: 100, category: 'home' },
  { id: 2, name: 'Office', lat: 52.24, lon: 21.02, radius_meters: 100, category: 'work' },
];

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

function installShell(status: Partial<TrackerStatus> = {}, scanner = true, version = '0.7.0') {
  setUserAgent(`${PLAIN_SAFARI} IstotaApp/${version}`);
  const merged = { ...STATUS, ...status };
  const tracker = {
    configure: vi.fn().mockResolvedValue(undefined),
    start: vi.fn().mockResolvedValue(merged),
    stop: vi.fn().mockResolvedValue({ ...merged, tracking: false }),
    status: vi.fn().mockResolvedValue(merged),
    sendNow: vi.fn().mockResolvedValue({ sent: 12 }),
    requestPermissions: vi.fn().mockResolvedValue({ authorization: 'always' }),
    openAppSettings: vi.fn().mockResolvedValue(undefined),
    configureWifiZone: vi.fn().mockResolvedValue(merged),
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
  placesMock.mockReset();
  placesMock.mockResolvedValue({ places: PLACES });
  createDeviceMock.mockReset();
  createDeviceMock.mockResolvedValue(MINTED);
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
    // Field wraps its label text and its slot in one <label>, and a
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
  it('offers one-tap setup and a scan when the device has no token yet', async () => {
    installShell({ configured: false });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText(/location tracking is off on this phone/i)).toBeTruthy();
    expect(screen.getByText('Turn on location tracking')).toBeTruthy();
    expect(screen.getByText('Scan a code instead')).toBeTruthy();
    expect(screen.queryByText('Send now')).toBeNull();
  });

  it('configures the tracker from a scanned code', async () => {
    const { tracker, qr } = installShell({ configured: false });
    const provisioning = { endpoint: 'https://example.invalid/webhooks/location', token: 'tok' };
    qr.scan.mockResolvedValue({ value: encodeProvisioning(provisioning) });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Scan a code instead'));
    await settle();
    expect(tracker.configure).toHaveBeenCalledWith(provisioning);
    expect(createDeviceMock).not.toHaveBeenCalled();
  });

  it('says so when the code is not one of ours', async () => {
    const { tracker, qr } = installShell({ configured: false });
    qr.scan.mockResolvedValue({ value: 'https://example.com/an-unrelated-qr' });
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByText('Scan a code instead'));
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
    await fireEvent.click(screen.getByText('Scan a code instead'));
    await settle();
    expect(screen.queryByText(/not an istota provisioning code/i)).toBeNull();
  });

  it('still offers one-tap setup on a shell that cannot scan', async () => {
    // 0.6.0 has the tracker and no scanner. Before one-tap setup that shell
    // could only be told to update; now it needs no code at all.
    installShell({ configured: false }, false);
    render(DeviceTrackerCard);
    await settle();
    expect(screen.queryByText('Scan a code instead')).toBeNull();
    expect(screen.getByText('Turn on location tracking')).toBeTruthy();
  });
});

/**
 * One-tap setup: mint a token for this phone, configure the tracker with it,
 * get permission, start.
 *
 * `status` answers in sequence, because the flow is driven by what the device
 * reports between steps: unconfigured before the mint, configured afterwards,
 * and authorization leaving `notDetermined` only once the user answers.
 */
describe('turning tracking on', () => {
  const UNCONFIGURED: Partial<TrackerStatus> = {
    configured: false,
    tracking: false,
    authorization: 'notDetermined',
    deviceId: '6F1C0D2A-1B2C-4D3E-8F90-0A1B2C3D4E5F',
  };

  /** Status answers in order: the mount read, then each read after it. The last repeats. */
  function statusSequence(
    tracker: ReturnType<typeof installShell>['tracker'],
    ...answers: Partial<TrackerStatus>[]
  ) {
    const full = answers.map((a) => ({ ...STATUS, ...UNCONFIGURED, ...a }));
    let i = 0;
    tracker.status.mockImplementation(async () => full[Math.min(i++, full.length - 1)]);
  }

  async function tapEnable() {
    await fireEvent.click(screen.getByText('Turn on location tracking'));
  }

  afterEach(() => {
    vi.useRealTimers();
  });

  it('mints a token for this phone and starts at once when already allowed', async () => {
    const { tracker } = installShell(UNCONFIGURED);
    statusSequence(tracker, {}, { configured: true, authorization: 'always' });
    tracker.start.mockResolvedValue({ ...STATUS, tracking: true, authorization: 'always' });
    const onDeviceAdded = vi.fn();
    render(DeviceTrackerCard, { onDeviceAdded });
    await settle();
    await tapEnable();
    await settle();
    expect(createDeviceMock).toHaveBeenCalledWith({
      kind: 'app',
      device_id: UNCONFIGURED.deviceId,
      label: 'iPhone',
    });
    // The endpoint without a query string and the token apart: the tracker
    // sends it as a Bearer header, so the app path never puts it in a URL.
    expect(tracker.configure).toHaveBeenCalledWith({
      endpoint: MINTED.endpoint,
      token: MINTED.token,
    });
    expect(tracker.requestPermissions).not.toHaveBeenCalled();
    expect(tracker.start).toHaveBeenCalledWith({ profile: 'detailed' });
    expect(onDeviceAdded).toHaveBeenCalled();
    expect(screen.getByText('Tracking is on.')).toBeTruthy();
  });

  it('asks for permission, polls for the answer, and starts once granted', async () => {
    vi.useFakeTimers();
    const { tracker } = installShell(UNCONFIGURED);
    statusSequence(
      tracker,
      {},
      { configured: true },
      { configured: true },
      { configured: true },
      { configured: true, authorization: 'whenInUse' },
    );
    tracker.start.mockResolvedValue({ ...STATUS, tracking: true, authorization: 'whenInUse' });
    render(DeviceTrackerCard);
    await vi.advanceTimersByTimeAsync(0);
    await tapEnable();
    await vi.advanceTimersByTimeAsync(0);
    expect(tracker.requestPermissions).toHaveBeenCalledTimes(1);
    expect(tracker.start).not.toHaveBeenCalled();
    // Two polls still see notDetermined, the third sees the answer.
    await vi.advanceTimersByTimeAsync(2000);
    expect(tracker.start).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    expect(tracker.start).toHaveBeenCalledWith({ profile: 'detailed' });
    expect(screen.getByText(/tracking is on\. ios will ask again later/i)).toBeTruthy();
  });

  it('gives up after a minute and leaves the tracker configured and stopped', async () => {
    vi.useFakeTimers();
    const { tracker } = installShell(UNCONFIGURED);
    statusSequence(tracker, {}, { configured: true });
    render(DeviceTrackerCard);
    await vi.advanceTimersByTimeAsync(0);
    await tapEnable();
    await vi.advanceTimersByTimeAsync(0);
    const readsAfterPrompt = tracker.status.mock.calls.length;
    await vi.advanceTimersByTimeAsync(61_000);
    expect(tracker.start).not.toHaveBeenCalled();
    // About one read a second, and none once the minute is up.
    const polls = tracker.status.mock.calls.length - readsAfterPrompt;
    expect(polls).toBeGreaterThanOrEqual(59);
    expect(polls).toBeLessThanOrEqual(62);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(tracker.status.mock.calls.length - readsAfterPrompt).toBe(polls);
    // The card's own permission controls take over.
    expect(screen.getByText('Allow location')).toBeTruthy();
    expect(screen.getByText(/has not been granted yet/i)).toBeTruthy();
  });

  it('stops polling when the card is destroyed', async () => {
    vi.useFakeTimers();
    const { tracker } = installShell(UNCONFIGURED);
    statusSequence(tracker, {}, { configured: true });
    const { unmount } = render(DeviceTrackerCard);
    await vi.advanceTimersByTimeAsync(0);
    await tapEnable();
    await vi.advanceTimersByTimeAsync(2000);
    const readsAtUnmount = tracker.status.mock.calls.length;
    unmount();
    await vi.advanceTimersByTimeAsync(60_000);
    // At most the one read `run()` makes as the cancelled action ends.
    expect(tracker.status.mock.calls.length - readsAtUnmount).toBeLessThanOrEqual(1);
    expect(tracker.start).not.toHaveBeenCalled();
  });

  it('does not prompt when access is denied', async () => {
    const { tracker } = installShell(UNCONFIGURED);
    statusSequence(tracker, {}, { configured: true, authorization: 'denied' });
    render(DeviceTrackerCard);
    await settle();
    await tapEnable();
    await settle();
    expect(tracker.configure).toHaveBeenCalled();
    expect(tracker.requestPermissions).not.toHaveBeenCalled();
    expect(tracker.start).not.toHaveBeenCalled();
    expect(screen.getByText('Open iOS Settings')).toBeTruthy();
  });

  it('shows a refused mint and leaves the tracker alone', async () => {
    const { tracker } = installShell(UNCONFIGURED);
    createDeviceMock.mockRejectedValue(
      new Error('This account already has 20 devices. Remove one first.'),
    );
    render(DeviceTrackerCard);
    await settle();
    await tapEnable();
    await settle();
    expect(screen.getByText('This account already has 20 devices. Remove one first.')).toBeTruthy();
    expect(tracker.configure).not.toHaveBeenCalled();
    expect(tracker.start).not.toHaveBeenCalled();
  });

  it('shows the plugin message when the token is for another address', async () => {
    const { tracker } = installShell(UNCONFIGURED);
    const message =
      'This code is for a different server or address than the one this app is connected to.';
    tracker.configure.mockRejectedValue(
      Object.assign(new Error(message), { code: 'WRONG_SERVER' }),
    );
    const onDeviceAdded = vi.fn();
    render(DeviceTrackerCard, { onDeviceAdded });
    await settle();
    await tapEnable();
    await settle();
    expect(screen.getByText(message)).toBeTruthy();
    expect(tracker.start).not.toHaveBeenCalled();
    // The row was minted before configure refused, so the list still shows it.
    expect(onDeviceAdded).toHaveBeenCalled();
  });

  it('says in plain words that the tracker could not store its credentials', async () => {
    const { tracker } = installShell(UNCONFIGURED);
    tracker.configure.mockRejectedValue(
      Object.assign(new Error('SecItemAdd failed: -34018'), { code: 'STORE_FAILED' }),
    );
    render(DeviceTrackerCard);
    await settle();
    await tapEnable();
    await settle();
    expect(screen.getByText('The tracker could not save its credentials. Try again.')).toBeTruthy();
    expect(screen.queryByText(/SecItemAdd/)).toBeNull();
  });
});

describe('a rejected token', () => {
  it('offers to set the phone up again, without touching tracking', async () => {
    const { tracker } = installShell({
      tracking: true,
      // GLManager's wording for a 401 or 403.
      lastError: 'HTTP 403 — token rejected, re-provision this device',
    });
    const onDeviceAdded = vi.fn();
    render(DeviceTrackerCard, { onDeviceAdded });
    await settle();
    expect(screen.getByText(/no longer accepts this phone's token/i)).toBeTruthy();
    // Rescan stays beside Send now.
    expect(screen.getByText('Rescan code')).toBeTruthy();
    await fireEvent.click(screen.getByText('Set up again'));
    await settle();
    expect(createDeviceMock).toHaveBeenCalledWith({
      kind: 'app',
      device_id: STATUS.deviceId,
      label: 'iPhone',
    });
    expect(tracker.configure).toHaveBeenCalledWith({
      endpoint: MINTED.endpoint,
      token: MINTED.token,
    });
    expect(onDeviceAdded).toHaveBeenCalled();
    expect(tracker.start).not.toHaveBeenCalled();
    expect(tracker.stop).not.toHaveBeenCalled();
    expect(tracker.requestPermissions).not.toHaveBeenCalled();
  });

  it('is not offered for any other error', async () => {
    installShell({ lastError: 'The request timed out.' });
    render(DeviceTrackerCard);
    await settle();
    expect(screen.queryByText('Set up again')).toBeNull();
  });
});

describe('the wifi zone', () => {
  // A bits-ui Select cannot be opened under jsdom (it needs pointer-capture
  // APIs the environment does not implement), so these assert on what the card
  // renders and on what it asks the device for, never by driving the dropdown.
  // Same constraint the profile-picker tests work around, recorded here so the
  // absence of a click-through test does not read as an oversight.

  it('is absent on a shell that predates it, rather than shown and broken', async () => {
    installShell({ currentWifi: 'HomeNet' }, true, '0.8.0');
    render(DeviceTrackerCard);
    await settle();
    expect(screen.queryByText('Wifi zone')).toBeNull();
    // And it does not go asking the server for places it has no use for.
    expect(placesMock).not.toHaveBeenCalled();
  });

  it('appears on a new enough shell and loads places to pin to', async () => {
    installShell({ currentWifi: 'HomeNet' }, true, '0.9.0');
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText('Wifi zone')).toBeTruthy();
    expect(placesMock).toHaveBeenCalled();
  });

  it('names both causes when no network name is readable', async () => {
    // The capability failure is invisible from the device's side — it looks
    // exactly like being off wifi — so the card cannot pick one and must say
    // both. Getting this wrong leaves a zone that silently never matches.
    installShell({ currentWifi: null }, true, '0.9.0');
    render(DeviceTrackerCard);
    await settle();
    // One sentence covering both: join a network (you may not be on one) and
    // update the app (the build may not be able to read the name at all).
    expect(screen.getByText(/join a wifi network.*update the app/i)).toBeTruthy();
  });

  it('says a configured zone is doing nothing while the name is unreadable', async () => {
    installShell({ currentWifi: null, wifiZoneSsid: 'HomeNet' }, true, '0.9.0');
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText(/does nothing until it can/i)).toBeTruthy();
  });

  it('says which network a zone is set for when on a different one', async () => {
    installShell(
      { currentWifi: 'CafeNet', wifiZoneSsid: 'HomeNet', wifiZoneActive: false },
      true,
      '0.9.0',
    );
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText(/set for HomeNet; this phone is on CafeNet/i)).toBeTruthy();
  });

  it('is silent when the zone is set and active', async () => {
    installShell(
      {
        currentWifi: 'HomeNet',
        wifiZoneSsid: 'HomeNet',
        wifiZoneActive: true,
        wifiZoneLatitude: 52.23,
        wifiZoneLongitude: 21.01,
      },
      true,
      '0.9.0',
    );
    render(DeviceTrackerCard);
    await settle();
    expect(screen.queryByText(/this phone is on/i)).toBeNull();
    expect(screen.queryByText(/does nothing/i)).toBeNull();
  });

  it('is a search field that narrows a long list to the matching places', async () => {
    // A dropdown of every place could not be used on a phone once the list ran
    // past a screenful; typing narrows it to what matches.
    placesMock.mockResolvedValue({
      places: [
        ...PLACES,
        ...Array.from({ length: 40 }, (_, i) => ({
          ...PLACES[0],
          id: 100 + i,
          name: `Cafe ${i}`,
        })),
      ],
    });
    installShell({ currentWifi: 'HomeNet' }, true, '0.9.0');
    render(DeviceTrackerCard);
    await settle();
    const field = screen.getByRole('combobox', { name: 'Wifi zone place' });
    await fireEvent.input(field, { target: { value: 'off' } });
    expect(screen.getAllByRole('option').map((o) => o.textContent?.trim())).toEqual(['Office']);
  });

  it('pins the zone to the place picked from the matches', async () => {
    const { tracker } = installShell({ currentWifi: 'HomeNet' }, true, '0.9.0');
    render(DeviceTrackerCard);
    await settle();
    const field = screen.getByRole('combobox', { name: 'Wifi zone place' });
    await fireEvent.input(field, { target: { value: 'off' } });
    await fireEvent.mouseDown(screen.getByRole('option', { name: 'Office' }));
    await settle();
    expect(tracker.configureWifiZone).toHaveBeenCalledWith({
      ssid: 'HomeNet',
      latitude: 52.24,
      longitude: 21.02,
    });
  });

  it('typing text that names no place pins nothing and puts the stored zone back', async () => {
    const { tracker } = installShell(
      {
        currentWifi: 'HomeNet',
        wifiZoneSsid: 'HomeNet',
        wifiZoneLatitude: 52.23,
        wifiZoneLongitude: 21.01,
      },
      true,
      '0.9.0',
    );
    render(DeviceTrackerCard);
    await settle();
    const field = screen.getByRole('combobox', { name: 'Wifi zone place' });
    expect(field).toHaveValue('Home');
    await fireEvent.input(field, { target: { value: 'nowhere' } });
    await fireEvent.blur(field);
    expect(field).toHaveValue('Home');
    expect(tracker.configureWifiZone).not.toHaveBeenCalled();
  });

  it('turns a set zone off from its own button', async () => {
    const { tracker } = installShell(
      {
        currentWifi: 'HomeNet',
        wifiZoneSsid: 'HomeNet',
        wifiZoneLatitude: 52.23,
        wifiZoneLongitude: 21.01,
      },
      true,
      '0.9.0',
    );
    render(DeviceTrackerCard);
    await settle();
    await fireEvent.click(screen.getByRole('button', { name: 'Turn off' }));
    await settle();
    expect(tracker.configureWifiZone).toHaveBeenCalledWith({ ssid: '', latitude: 0, longitude: 0 });
  });

  it('explains itself rather than showing an empty picker when places fail to load', async () => {
    placesMock.mockRejectedValue(new Error('nope'));
    installShell({ currentWifi: 'HomeNet' }, true, '0.9.0');
    render(DeviceTrackerCard);
    await settle();
    expect(screen.getByText(/could not load your places/i)).toBeTruthy();
  });
});

/**
 * The seam, not the ladder — `dateFormat.test.ts` owns which rung a given age
 * lands on. What is asserted here is that this row reaches the shared helper
 * with the options this card is supposed to pass, since those are the two
 * things a conversion silently gets wrong.
 */
describe('the last-sent row', () => {
  const lastSent = (): HTMLElement => {
    const dt = screen.getByText('Last sent');
    const dd = dt.nextElementSibling as HTMLElement | null;
    if (!dd) throw new Error('Last sent has no value beside it');
    return dd;
  };

  /** The relative rung alone, without the exact-instant line beneath it. */
  const rung = (): string =>
    lastSent()
      .textContent?.replace(lastSent().querySelector('.sent-exact')?.textContent ?? '', '')
      .trim() ?? '';

  it('renders the tight ladder rather than the spaced one it used to', async () => {
    // `5 min ago` and `2 h ago` were this card's alone; every other surface
    // rendered `5m ago`. Rounding went with the spacing — 90 seconds read as
    // "2 min ago" here, a figure larger than the time that had elapsed.
    installShell({ lastSentAt: new Date(Date.now() - 90_000).toISOString() });
    render(DeviceTrackerCard);
    await settle();
    expect(rung()).toBe('1m ago');
  });

  it('keeps its own empty fallback', async () => {
    installShell({ lastSentAt: null });
    render(DeviceTrackerCard);
    await settle();
    expect(rung()).toBe('never');
  });

  it('shows the exact instant on the surface, not behind a hover', async () => {
    // The absolute timestamp this row fell back to past a day is now a second
    // line, so the "when did it stop" question a stalled queue raises is still
    // answerable — and answerable on a touch screen, which is the only place
    // this card mounts. `formatDateTime` rather than a restatement of it, so
    // swapping the component to another formatter fails here.
    const iso = new Date(Date.now() - 3 * 86_400_000).toISOString();
    installShell({ lastSentAt: iso });
    render(DeviceTrackerCard);
    await settle();
    expect(rung()).toBe('3d ago');
    const exact = lastSent().querySelector('.sent-exact');
    expect(exact?.textContent?.trim()).toBe(formatDateTime(iso));
  });

  it('offers no dangling exact line when there is no instant to carry', async () => {
    installShell({ lastSentAt: null });
    render(DeviceTrackerCard);
    await settle();
    expect(lastSent().querySelector('.sent-exact')).toBeNull();
  });
});
