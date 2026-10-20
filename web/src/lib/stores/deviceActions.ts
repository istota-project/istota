import { createDevice, deleteDevice, getProfile, listDevices, type DeviceScope } from '$lib/api';
import { deviceModel } from '$lib/platform/native';
import {
  actionsAvailable,
  actionsStatus,
  forgetActions,
  provisionActions,
} from '$lib/platform/nativeActions';

/**
 * Keep the shell's device token in step with what the account wants it to do.
 *
 * Runs after a live `getMe` and again whenever `quick_notes_folder` changes.
 * The web mints, the shell keeps: a token reaches the shell once and is never
 * read back, so "the token is fine" is judged from the scopes the shell reports
 * and whether its last send was refused. A failure is logged and swallowed; the
 * next page load tries again.
 */

// The shell's status does not report the folder label it holds, so this page
// remembers what it last told the shell. Unknown after a load, which costs one
// label-only provision per page load.
let provisionedFolder: { deviceId: string; folder: string | null } | null = null;
let chain: Promise<void> = Promise.resolve();

export function syncDeviceActions(): Promise<void> {
  // Serialised so a settings save racing the page-load sync cannot mint twice.
  const run = chain.then(sync).catch(() => {
    console.warn('[istota] Could not sync the native actions token.');
  });
  chain = run;
  return run;
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

  const wanted: DeviceScope[] = [];
  if (folder) wanted.push('notes');

  if (wanted.length === 0) {
    if (status.scopes.length === 0) return;
    await forgetActions();
    provisionedFolder = null;
    const { devices } = await listDevices();
    const deviceId = status.deviceId.toUpperCase();
    for (const device of devices) {
      if (device.device_id.toUpperCase() === deviceId) await deleteDevice(device.id);
    }
    return;
  }

  if (sameScopes(status.scopes, wanted) && !status.tokenRejected) {
    const known =
      provisionedFolder !== null &&
      provisionedFolder.deviceId === status.deviceId &&
      provisionedFolder.folder === folder;
    if (known) return;
    await provisionActions(null, wanted, folder);
    provisionedFolder = { deviceId: status.deviceId, folder };
    return;
  }

  const minted = await createDevice({
    device_id: status.deviceId,
    label: deviceModel(),
    scopes: wanted,
  });
  await provisionActions(minted.token, minted.scopes, folder);
  provisionedFolder = { deviceId: status.deviceId, folder };
}

/** Forget what this page has told the shell. Tests reset between cases with it. */
export function resetDeviceActions(): void {
  provisionedFolder = null;
  chain = Promise.resolve();
}
