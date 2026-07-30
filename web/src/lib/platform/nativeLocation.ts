/**
 * The background location tracker, as the settings page sees it.
 *
 * The tracker is a native plugin in the iOS shell (`IstotaLocation`) that logs
 * fixes and POSTs them to `/webhooks/location` on its own schedule, with no
 * involvement from this app at runtime. What crosses this facade is only
 * configuration and status — provision the device, turn it on, ask whether it
 * is still alive.
 *
 * Its settings live on the device and nowhere else, which is forced rather
 * than chosen: one account can have two phones (Open question 4 in the spec),
 * so a tracker setting kept server-side would have the two overwriting each
 * other's row. That is why there is a facade here at all instead of a profile
 * field.
 *
 * Every export degrades to "not available" off-shell, so `/location/settings`
 * renders in a browser exactly as it did before this existed. See `native.ts`
 * for why the User Agent, not `window.Capacitor`, is the signal — and for why
 * calling a plugin needs no bundled `@capacitor/core`: the injected bridge
 * populates `Capacitor.Plugins` for natively-registered plugins even when the
 * page is remote.
 */

import { shellAtLeast } from './native';
import { decodeProvisioning, type Provisioning } from '$lib/location/provisioning';

/** Shell 0.6.0 shipped the tracker with `configured`/`endpointHost`/`deviceId`. */
const SHELL_WITH_TRACKER = '0.6.0';
/** Shell 0.7.0 shipped `IstotaQrScanner`. */
const SHELL_WITH_SCANNER = '0.7.0';

export type TrackingProfile = 'detailed' | 'places';

export interface TrackerStatus {
  tracking: boolean;
  profile: TrackingProfile;
  authorization: 'always' | 'whenInUse' | 'denied' | 'notDetermined';
  queuedPoints: number;
  /** Last *success*. The send throttle keeps its own clock; this one reports. */
  lastSentAt: string | null;
  lastError: string | null;
  droppedPoints: number;
  configured: boolean;
  /** Host only — the token is never echoed back, matching the server-side rule. */
  endpointHost: string | null;
  deviceId: string;
}

interface LocationPlugins {
  IstotaLocation?: {
    configure(opts: { endpoint: string; token: string }): Promise<void>;
    start(opts?: { profile?: TrackingProfile }): Promise<TrackerStatus>;
    stop(): Promise<TrackerStatus>;
    status(): Promise<TrackerStatus>;
    sendNow(): Promise<{ sent: number }>;
    requestPermissions(): Promise<{ authorization: string }>;
    openAppSettings(): Promise<void>;
  };
  IstotaQrScanner?: {
    scan(): Promise<{ value: string | null }>;
  };
}

function plugins(): LocationPlugins | null {
  if (typeof window === 'undefined') return null;
  return (window as { Capacitor?: { Plugins?: LocationPlugins } }).Capacitor?.Plugins ?? null;
}

/** Can this client run a background tracker at all? */
export function trackerAvailable(): boolean {
  return shellAtLeast(SHELL_WITH_TRACKER) && !!plugins()?.IstotaLocation;
}

/**
 * Can it scan a code, or does the user provision some other way?
 *
 * Gated separately from the tracker: 0.6.0 has the tracker and no scanner, and
 * that shell should show a working status card that simply cannot rescan,
 * rather than no card at all.
 */
export function scannerAvailable(): boolean {
  return shellAtLeast(SHELL_WITH_SCANNER) && !!plugins()?.IstotaQrScanner;
}

/**
 * The whole readout, or null off-shell.
 *
 * Null and "not tracking" are different answers and the card renders them
 * differently — a browser gets the stand-in line, a phone gets an Off switch.
 */
export async function trackerStatus(): Promise<TrackerStatus | null> {
  const tracker = plugins()?.IstotaLocation;
  if (!tracker || !trackerAvailable()) return null;
  return tracker.status();
}

/**
 * Start, or switch profile on an already-running tracker.
 *
 * `start` re-arms in place, so changing profile costs no gap in coverage and
 * the card has one call for both. Rejects when location authorization is
 * missing rather than reporting a tracker that runs and logs nothing.
 */
export async function startTracking(profile?: TrackingProfile): Promise<TrackerStatus | null> {
  const tracker = plugins()?.IstotaLocation;
  if (!tracker) return null;
  return tracker.start(profile ? { profile } : undefined);
}

export async function stopTracking(): Promise<TrackerStatus | null> {
  const tracker = plugins()?.IstotaLocation;
  if (!tracker) return null;
  return tracker.stop();
}

/** Flush the queue now. Returns how many points the server took. */
export async function sendNow(): Promise<number> {
  const tracker = plugins()?.IstotaLocation;
  if (!tracker) return 0;
  return (await tracker.sendNow()).sent;
}

/**
 * Ask for location permission.
 *
 * Resolves with the status as it stands rather than waiting on the prompt, and
 * iOS refuses the Always prompt as a first ask, so this is normally called
 * twice — When In Use first, then Always on the second tap.
 */
export async function requestPermissions(): Promise<string | null> {
  const tracker = plugins()?.IstotaLocation;
  if (!tracker) return null;
  return (await tracker.requestPermissions()).authorization;
}

/**
 * Open this app's page in iOS Settings.
 *
 * The only way back from a denied Always authorization: it cannot be
 * re-requested in-app, so without this the card is a dead end and the tracker
 * has silently stopped.
 */
export async function openAppSettings(): Promise<void> {
  await plugins()?.IstotaLocation?.openAppSettings();
}

export async function configureTracker(p: Provisioning): Promise<void> {
  const tracker = plugins()?.IstotaLocation;
  if (!tracker) throw new Error('No tracker on this device.');
  await tracker.configure({ endpoint: p.endpoint, token: p.token });
}

/** A cancelled scan is not a failure — the same shape as the pickers. */
function isCancellation(e: unknown): boolean {
  const message = e instanceof Error ? e.message : String(e ?? '');
  return /cancel/i.test(message);
}

/**
 * Scan a provisioning code.
 *
 * Returns null when the user backed out, and throws when the camera itself
 * refused (denied permission, no camera) — the card can only say something
 * useful about the second. A code that scans but is not one of ours resolves
 * to null from the decoder, which the caller reports as "not recognised";
 * distinguishing that from a cancel is why the decode does not happen here.
 */
export async function scanProvisioning(): Promise<{ value: string | null } | null> {
  const scanner = plugins()?.IstotaQrScanner;
  if (!scanner) return null;
  try {
    const result = await scanner.scan();
    return { value: result.value ?? null };
  } catch (e) {
    if (isCancellation(e)) return { value: null };
    throw e;
  }
}

/** Scan and parse in one step, for the card's single "Scan" button. */
export async function scanAndDecode(): Promise<
  { ok: true; provisioning: Provisioning } | { ok: false; reason: 'cancelled' | 'unrecognised' }
> {
  const scanned = await scanProvisioning();
  if (!scanned || scanned.value === null) return { ok: false, reason: 'cancelled' };
  const provisioning = decodeProvisioning(scanned.value);
  if (!provisioning) return { ok: false, reason: 'unrecognised' };
  return { ok: true, provisioning };
}
