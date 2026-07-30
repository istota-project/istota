import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  trackerAvailable,
  scannerAvailable,
  trackerStatus,
  startTracking,
  stopTracking,
  sendNow,
  requestPermissions,
  openAppSettings,
  configureTracker,
  scanAndDecode,
  wifiZoneAvailable,
  configureWifiZone,
  type TrackerStatus,
} from './nativeLocation';
import { encodeProvisioning } from '$lib/location/provisioning';

const PLAIN_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1';

function setUserAgent(ua: string): void {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
}

function shell(version: string): void {
  setUserAgent(`${PLAIN_SAFARI} IstotaApp/${version}`);
}

const STATUS: TrackerStatus = {
  tracking: true,
  profile: 'detailed',
  authorization: 'always',
  queuedPoints: 7,
  lastSentAt: '2026-07-30T10:00:00Z',
  lastError: null,
  droppedPoints: 0,
  configured: true,
  endpointHost: 'example.invalid',
  deviceId: 'ABC-123',
};

function installPlugins(p: Record<string, unknown>): void {
  (globalThis as Record<string, unknown>).Capacitor = { Plugins: p };
}

function fakeTracker(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    configure: vi.fn().mockResolvedValue(undefined),
    start: vi.fn().mockResolvedValue(STATUS),
    stop: vi.fn().mockResolvedValue({ ...STATUS, tracking: false }),
    status: vi.fn().mockResolvedValue(STATUS),
    sendNow: vi.fn().mockResolvedValue({ sent: 7 }),
    requestPermissions: vi.fn().mockResolvedValue({ authorization: 'always' }),
    openAppSettings: vi.fn().mockResolvedValue(undefined),
    configureWifiZone: vi.fn().mockResolvedValue(STATUS),
    ...overrides,
  };
}

const GOOD = { endpoint: 'https://example.invalid/webhooks/location', token: 'a'.repeat(43) };

beforeEach(() => {
  shell('0.7.0');
  installPlugins({ IstotaLocation: fakeTracker(), IstotaQrScanner: { scan: vi.fn() } });
});

afterEach(() => {
  setUserAgent(PLAIN_SAFARI);
  delete (globalThis as Record<string, unknown>).Capacitor;
  vi.restoreAllMocks();
});

describe('availability gating', () => {
  it('is unavailable in a plain browser even with a bridge shim present', () => {
    setUserAgent(PLAIN_SAFARI);
    expect(trackerAvailable()).toBe(false);
    expect(scannerAvailable()).toBe(false);
  });

  it('is unavailable on a shell too old to carry the plugin', () => {
    shell('0.5.0');
    expect(trackerAvailable()).toBe(false);
  });

  it('separates the two gates, so 0.6.0 shows a working card that cannot rescan', () => {
    shell('0.6.0');
    expect(trackerAvailable()).toBe(true);
    expect(scannerAvailable()).toBe(false);
  });

  it('is unavailable when the version is new enough but the plugin is missing', () => {
    installPlugins({});
    expect(trackerAvailable()).toBe(false);
    expect(scannerAvailable()).toBe(false);
  });
});

describe('trackerStatus', () => {
  it('reads the plugin through', async () => {
    await expect(trackerStatus()).resolves.toEqual(STATUS);
  });

  it('answers null off-shell rather than throwing', async () => {
    // Null and "not tracking" are different answers: the card renders a
    // stand-in line for one and an Off switch for the other.
    setUserAgent(PLAIN_SAFARI);
    await expect(trackerStatus()).resolves.toBeNull();
  });
});

describe('startTracking', () => {
  it('passes a profile through', async () => {
    const plugins = (
      globalThis as { Capacitor?: { Plugins?: Record<string, ReturnType<typeof fakeTracker>> } }
    ).Capacitor!.Plugins!;
    await startTracking('places');
    expect(plugins.IstotaLocation.start).toHaveBeenCalledWith({ profile: 'places' });
  });

  it('omits the argument entirely when no profile is asked for', async () => {
    // The plugin keeps the stored profile when none is supplied; sending
    // `{profile: undefined}` would be a different call to reason about.
    const plugins = (
      globalThis as { Capacitor?: { Plugins?: Record<string, ReturnType<typeof fakeTracker>> } }
    ).Capacitor!.Plugins!;
    await startTracking();
    expect(plugins.IstotaLocation.start).toHaveBeenCalledWith(undefined);
  });

  it('returns the status the plugin resolves with, saving a round trip', async () => {
    await expect(startTracking()).resolves.toEqual(STATUS);
  });
});

describe('sendNow', () => {
  it('reports how many points the server took', async () => {
    await expect(sendNow()).resolves.toBe(7);
  });

  it('is zero when the plugin is absent', async () => {
    installPlugins({});
    await expect(sendNow()).resolves.toBe(0);
  });
});

describe('configureTracker', () => {
  it('hands both halves to the plugin', async () => {
    const plugins = (
      globalThis as { Capacitor?: { Plugins?: Record<string, ReturnType<typeof fakeTracker>> } }
    ).Capacitor!.Plugins!;
    await configureTracker(GOOD);
    expect(plugins.IstotaLocation.configure).toHaveBeenCalledWith(GOOD);
  });

  it('throws when the plugin is absent, rather than reporting a device that was never configured', async () => {
    installPlugins({});
    await expect(configureTracker(GOOD)).rejects.toThrow(/no tracker/i);
  });
});

describe('every export is version-gated, not merely plugin-gated', () => {
  // The two are not the same test. A page can carry a `window.Capacitor` shim
  // without being in the shell at all — that is the premise of the very first
  // test in this file — so a check for the plugin alone would let a browser
  // call straight into it. Each case below leaves the plugins installed and
  // downgrades only the User Agent.
  beforeEach(() => {
    setUserAgent(PLAIN_SAFARI);
    installPlugins({ IstotaLocation: fakeTracker(), IstotaQrScanner: { scan: vi.fn() } });
  });

  it('trackerStatus answers null', async () => {
    await expect(trackerStatus()).resolves.toBeNull();
  });

  it('startTracking answers null and never reaches the plugin', async () => {
    const plugins = (
      globalThis as { Capacitor?: { Plugins?: Record<string, ReturnType<typeof fakeTracker>> } }
    ).Capacitor!.Plugins!;
    await expect(startTracking('places')).resolves.toBeNull();
    expect(plugins.IstotaLocation.start).not.toHaveBeenCalled();
  });

  it('stopTracking answers null', async () => {
    await expect(stopTracking()).resolves.toBeNull();
  });

  it('sendNow answers zero', async () => {
    await expect(sendNow()).resolves.toBe(0);
  });

  it('requestPermissions answers null', async () => {
    await expect(requestPermissions()).resolves.toBeNull();
  });

  it('openAppSettings never reaches the plugin', async () => {
    const plugins = (
      globalThis as { Capacitor?: { Plugins?: Record<string, ReturnType<typeof fakeTracker>> } }
    ).Capacitor!.Plugins!;
    await openAppSettings();
    expect(plugins.IstotaLocation.openAppSettings).not.toHaveBeenCalled();
  });

  it('configureTracker throws rather than silently doing nothing', async () => {
    await expect(configureTracker(GOOD)).rejects.toThrow(/no tracker/i);
  });

  it('scanAndDecode reports unavailable', async () => {
    await expect(scanAndDecode()).resolves.toEqual({ ok: false, reason: 'unavailable' });
  });

  it('configureWifiZone answers null and never reaches the plugin', async () => {
    const plugins = (
      globalThis as { Capacitor?: { Plugins?: Record<string, ReturnType<typeof fakeTracker>> } }
    ).Capacitor!.Plugins!;
    await expect(
      configureWifiZone({ ssid: 'Home', latitude: 52.2, longitude: 21.0 }),
    ).resolves.toBeNull();
    expect(plugins.IstotaLocation.configureWifiZone).not.toHaveBeenCalled();
  });
});

describe('wifi zone', () => {
  it('is unavailable on a shell that predates it, even with the method present', () => {
    // The gate that matters. 0.8.x carries a tracker and no zone; calling into
    // it would reject every time, and the card would show a control that
    // cannot work rather than omitting it.
    shell('0.8.0');
    installPlugins({ IstotaLocation: fakeTracker() });
    expect(trackerAvailable()).toBe(true);
    expect(wifiZoneAvailable()).toBe(false);
  });

  it('is unavailable on a new enough shell whose plugin lacks the method', () => {
    shell('0.9.0');
    const tracker = fakeTracker();
    delete (tracker as Record<string, unknown>).configureWifiZone;
    installPlugins({ IstotaLocation: tracker });
    expect(wifiZoneAvailable()).toBe(false);
  });

  it('passes the zone through and returns what the device reports', async () => {
    shell('0.9.0');
    const zoned = { ...STATUS, wifiZoneSsid: 'Home', wifiZoneActive: true };
    const tracker = fakeTracker({ configureWifiZone: vi.fn().mockResolvedValue(zoned) });
    installPlugins({ IstotaLocation: tracker });

    await expect(
      configureWifiZone({ ssid: 'Home', latitude: 52.23, longitude: 21.01 }),
    ).resolves.toEqual(zoned);
    expect(tracker.configureWifiZone).toHaveBeenCalledWith({
      ssid: 'Home',
      latitude: 52.23,
      longitude: 21.01,
    });
  });

  it('clears with an empty SSID rather than an omitted one', async () => {
    // The native signature is total: a clear still carries coordinates, which
    // it ignores. Sending `undefined` would read as 0,0 off the coast of
    // Africa if the native side ever stopped special-casing the empty name.
    shell('0.9.0');
    const tracker = fakeTracker();
    installPlugins({ IstotaLocation: tracker });

    await configureWifiZone(null);
    expect(tracker.configureWifiZone).toHaveBeenCalledWith({
      ssid: '',
      latitude: 0,
      longitude: 0,
    });
  });

  it('propagates a refusal instead of swallowing it', async () => {
    // An out-of-range coordinate is refused natively rather than clamped, so
    // the caller has to see the rejection — a silently-dropped zone would look
    // exactly like one that was stored.
    shell('0.9.0');
    installPlugins({
      IstotaLocation: fakeTracker({
        configureWifiZone: vi.fn().mockRejectedValue(new Error('INVALID_WIFI_ZONE')),
      }),
    });
    await expect(configureWifiZone({ ssid: 'Home', latitude: 999, longitude: 21 })).rejects.toThrow(
      /INVALID_WIFI_ZONE/,
    );
  });
});

describe('stopTracking', () => {
  it('returns the status the plugin resolves with', async () => {
    await expect(stopTracking()).resolves.toEqual({ ...STATUS, tracking: false });
  });
});

describe('requestPermissions', () => {
  it('reports the authorization as it stands', async () => {
    // Resolves with the current state rather than waiting on the prompt, so
    // two calls is the normal shape (iOS refuses Always as a first ask).
    await expect(requestPermissions()).resolves.toBe('always');
  });
});

describe('openAppSettings', () => {
  it('reaches the plugin', async () => {
    const plugins = (
      globalThis as { Capacitor?: { Plugins?: Record<string, ReturnType<typeof fakeTracker>> } }
    ).Capacitor!.Plugins!;
    await openAppSettings();
    expect(plugins.IstotaLocation.openAppSettings).toHaveBeenCalled();
  });
});

describe('scanAndDecode', () => {
  function withScan(impl: () => Promise<{ value: string | null }>) {
    installPlugins({ IstotaLocation: fakeTracker(), IstotaQrScanner: { scan: vi.fn(impl) } });
  }

  it('decodes one of our codes', async () => {
    withScan(async () => ({ value: encodeProvisioning(GOOD) }));
    await expect(scanAndDecode()).resolves.toEqual({ ok: true, provisioning: GOOD });
  });

  it('distinguishes a cancel from an unrecognised code', async () => {
    // The card says nothing about a cancel and reports the other, so the two
    // must not collapse into one failure.
    withScan(async () => ({ value: null }));
    await expect(scanAndDecode()).resolves.toEqual({ ok: false, reason: 'cancelled' });

    withScan(async () => ({ value: 'https://example.com/some-other-qr' }));
    await expect(scanAndDecode()).resolves.toEqual({ ok: false, reason: 'unrecognised' });
  });

  it('lets a camera failure through, since only that is worth reporting', async () => {
    // The plugin resolves for a cancel and rejects only when the camera
    // refused, so there is no message-matching here to misclassify a future
    // error whose text happens to contain the word "cancel".
    withScan(async () => {
      throw new Error('Camera access is denied. Allow it in iOS Settings to scan a code.');
    });
    await expect(scanAndDecode()).rejects.toThrow(/camera access is denied/i);
  });

  it('reports an absent scanner as unavailable, not as a cancel', async () => {
    // Folding this into 'cancelled' would give a Scan button that appears to
    // do nothing, since the card says nothing at all about a cancel.
    installPlugins({ IstotaLocation: fakeTracker() });
    await expect(scanAndDecode()).resolves.toEqual({ ok: false, reason: 'unavailable' });
  });
});
