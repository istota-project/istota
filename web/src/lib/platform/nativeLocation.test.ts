import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  trackerAvailable,
  scannerAvailable,
  trackerStatus,
  startTracking,
  sendNow,
  configureTracker,
  scanAndDecode,
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

  it('is zero off-shell', async () => {
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

  it('throws off-shell rather than reporting a device that was never configured', async () => {
    installPlugins({});
    await expect(configureTracker(GOOD)).rejects.toThrow(/no tracker/i);
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

  it('treats a rejection naming a cancellation as a cancel', async () => {
    withScan(async () => {
      throw new Error('Scan cancelled by user');
    });
    await expect(scanAndDecode()).resolves.toEqual({ ok: false, reason: 'cancelled' });
  });

  it('lets a real camera failure through, since only that is worth reporting', async () => {
    withScan(async () => {
      throw new Error('Camera access denied');
    });
    await expect(scanAndDecode()).rejects.toThrow(/camera access denied/i);
  });

  it('reads as a cancel when there is no scanner at all', async () => {
    installPlugins({ IstotaLocation: fakeTracker() });
    await expect(scanAndDecode()).resolves.toEqual({ ok: false, reason: 'cancelled' });
  });
});
