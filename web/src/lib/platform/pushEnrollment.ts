import { listPushDevices, registerPushDevice } from '$lib/api';
import { shellVersion } from './native';
import { pushPermissions, registerForPush, type PushRegistration } from './nativePush';
import {
  appConnectionAvailable,
  connectionContext,
  recordPushRegistration,
  type NativeConnectionContext,
} from './nativeConnection';

/**
 * Stores this device's token on the server, bound to the app's connection when it has one,
 * and hands the registration ID back so the app can remove it on disconnect.
 */
export async function saveEnrollment(
  registration: PushRegistration & { token: string },
  context: NativeConnectionContext | null,
): Promise<void> {
  const saved = await registerPushDevice({
    token: registration.token,
    environment: registration.environment,
    platform: 'ios',
    app_version: shellVersion() ?? '',
    ...(context
      ? { connection_binding: context.connectionBinding, connection_generation: context.generation }
      : {}),
  });
  if (context) await recordPushRegistration(saved.id, context);
}

/**
 * Re-enrolls this device when its server row was written by another app version.
 *
 * An in-place upgrade keeps the APNs token, so a 0.11 row keeps receiving alerts without a
 * connection binding, and the 0.12 app drops every tap on them (ISSUE-791). Only a row that
 * already exists is refreshed: a device the user removed stays removed. Never prompts.
 * While another of the user's phones runs a different version, every load asks the app for
 * its token and finds no match; that native call is the whole cost.
 */
export async function refreshStaleEnrollment(): Promise<boolean> {
  const version = shellVersion();
  if (!appConnectionAvailable() || version === null) return false;
  try {
    if ((await pushPermissions())?.permission !== 'granted') return false;
    const { devices } = await listPushDevices();
    const stale = devices.filter((d) => d.platform === 'ios' && d.app_version !== version);
    if (stale.length === 0) return false;
    const context = await connectionContext();
    const registration = await registerForPush();
    const token = registration?.token;
    if (!context || !registration || registration.permission !== 'granted' || !token) return false;
    const mine = stale.some(
      (d) => d.environment === registration.environment && token.endsWith(d.token_tail),
    );
    if (!mine) return false;
    await saveEnrollment({ ...registration, token }, context);
    return true;
  } catch {
    console.warn('[istota] Could not refresh this device push enrollment.');
    return false;
  }
}
