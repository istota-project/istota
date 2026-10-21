import { createDevice, deleteDevice, getProfile, listDevices, type DeviceScope } from '$lib/api';
import { deviceModel, shellAtLeast } from '$lib/platform/native';
import {
  actionsAvailable,
  actionsStatus,
  forgetActions,
  provisionActions,
  SHELL_WITH_VOICE,
} from '$lib/platform/nativeActions';

/**
 * Keep the shell's device token in step with what the account wants it to do.
 *
 * Runs after a live `getMe` and again whenever `quick_notes_folder` or
 * `default_room` changes. A 0.14.0 shell always wants `voice`; `notes` only
 * when a folder is set.
 * The web mints, the shell keeps: a token reaches the shell once and is never
 * read back, so "the token is fine" is judged from the scopes the shell reports,
 * whether its last send was refused, and whether this account has a row for the
 * shell's device. That last check cannot tell two accounts' rows for one device
 * apart: when both have minted on this phone, the shell may hold the other's
 * token. A failure is logged and swallowed; the next page load tries again.
 */

// The shell's status does not report the labels it holds, so this page
// remembers what it last told the shell. Unknown after a load, which costs one
// label-only provision per page load.
let provisioned: { deviceId: string; folder: string | null; voiceRoom: string | null } | null =
  null;
let chain: Promise<void> = Promise.resolve();

export function syncDeviceActions(): Promise<void> {
  // Serialised so a settings save racing the page-load sync cannot mint twice.
  const run = chain.then(sync).catch(() => {
    console.warn('[istota] Could not sync the native actions token.');
  });
  chain = run;
  return run;
}

async function ownsDeviceRow(deviceId: string): Promise<boolean> {
  const { devices } = await listDevices();
  const wanted = deviceId.toUpperCase();
  return devices.some((phone) => phone.token && phone.device_id.toUpperCase() === wanted);
}

function sameScopes(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const have = [...a].sort();
  const want = [...b].sort();
  return have.every((scope, i) => scope === want[i]);
}

async function sync(): Promise<void> {
  if (!actionsAvailable()) return;
  const status = await actionsStatus();
  if (!status) return;
  const { profile } = await getProfile();
  const folder = profile?.quick_notes_folder || null;
  const voice = shellAtLeast(SHELL_WITH_VOICE);
  const voiceRoom = voice ? profile?.default_room_name || null : null;

  const wanted: DeviceScope[] = [];
  if (folder) wanted.push('notes');
  if (voice) wanted.push('voice');

  if (wanted.length === 0) {
    if (status.scopes.length === 0) return;
    await forgetActions();
    provisioned = null;
    const { devices } = await listDevices();
    const deviceId = status.deviceId.toUpperCase();
    // The device token only: turning quick notes off leaves the tracker sending.
    for (const phone of devices) {
      if (phone.token && phone.device_id.toUpperCase() === deviceId) {
        await deleteDevice(phone.token.id);
      }
    }
    return;
  }

  if (sameScopes(status.scopes, wanted) && !status.tokenRejected) {
    const known =
      provisioned !== null &&
      provisioned.deviceId === status.deviceId &&
      provisioned.folder === folder &&
      provisioned.voiceRoom === voiceRoom;
    if (known) return;
    // The shell keeps one token per connection, and the account signed in now
    // may not be the one that minted it. Without a row of ours it is not ours.
    if (await ownsDeviceRow(status.deviceId)) {
      await provisionActions(null, wanted, folder, voiceRoom);
      provisioned = { deviceId: status.deviceId, folder, voiceRoom };
      return;
    }
  }

  const minted = await createDevice({
    device_id: status.deviceId,
    label: deviceModel(),
    scopes: wanted,
  });
  await provisionActions(minted.token, minted.scopes, folder, voiceRoom);
  provisioned = { deviceId: status.deviceId, folder, voiceRoom };
}

/** Forget what this page has told the shell. Tests reset between cases with it. */
export function resetDeviceActions(): void {
  provisioned = null;
  chain = Promise.resolve();
}
