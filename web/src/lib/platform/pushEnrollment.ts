import {
  deletePushDevice,
  getMobilePushCapabilities,
  getPushSenderKey,
  listPushDevices,
  registerPushDevice,
  type PrivatePushEnrollment,
} from '$lib/api';
import { shellVersion } from './native';
import {
  cancelPrivateRegistration,
  commitPrivateRegistration,
  preparePrivateRegistration,
  privatePushAvailable,
  pushPermissions,
  registerForPush,
  type PreparedPrivateRegistration,
  type PushRegistration,
} from './nativePush';
import {
  appConnectionAvailable,
  connectionContext,
  recordPushRegistration,
  type NativeConnectionContext,
} from './nativeConnection';

/**
 * Whether the server offers private push to this app. A discovery read that fails counts as
 * no offer: a plain registration on a private installation gets the fixed alert, never text.
 */
async function privatePushOffered(): Promise<boolean> {
  if (!privatePushAvailable()) return false;
  try {
    const push = await getMobilePushCapabilities();
    return push.delivery === 'direct' && (push.private?.versions ?? []).includes(1);
  } catch {
    return false;
  }
}

function privateField(
  prepared: PreparedPrivateRegistration,
  senderKeyId: string,
): PrivatePushEnrollment {
  if (prepared.content === 'generic') {
    return { version: 1, content: 'generic', generation: prepared.generation };
  }
  if (
    prepared.content !== 'encrypted' ||
    !prepared.encryptionPublicKey ||
    !prepared.encryptionKeyId
  ) {
    throw new Error('The app returned an unusable private enrollment.');
  }
  return {
    version: 1,
    content: 'encrypted',
    encryption_public_key: prepared.encryptionPublicKey,
    encryption_key_id: prepared.encryptionKeyId,
    sender_key_id: senderKeyId,
    generation: prepared.generation,
  };
}

function senderKeyChanged(e: unknown): boolean {
  return (
    (e as { status?: unknown } | null)?.status === 409 &&
    (e as Error).message === 'sender key changed'
  );
}

/**
 * Sender key, native prepare, the registration with `private`, native commit. Anything that
 * fails after prepare cancels it, and a key rotated meanwhile restarts the flow once.
 */
async function savePrivateEnrollment(context: NativeConnectionContext): Promise<void> {
  const binding = context.connectionBinding;
  for (let attempt = 0; ; attempt++) {
    const sender = await getPushSenderKey();
    const prepared = await preparePrivateRegistration({
      binding,
      senderKey: sender.public_key,
      senderKeyId: sender.key_id,
    });
    if (!prepared) throw new Error('Private notifications are unavailable in this app build.');
    const pending = { binding, generation: prepared.generation };
    let saved: { id: number };
    try {
      saved = await registerPushDevice({
        token: prepared.token,
        environment: prepared.environment,
        platform: 'ios',
        app_version: shellVersion() ?? '',
        connection_binding: binding,
        connection_generation: context.generation,
        private: privateField(prepared, sender.key_id),
      });
    } catch (e) {
      await cancelPrivateRegistration(pending).catch(() => {});
      if (attempt === 0 && senderKeyChanged(e)) continue;
      throw e;
    }
    try {
      await commitPrivateRegistration({ ...pending, deviceId: saved.id });
    } catch (e) {
      // The server holds a key the app no longer has; remove the row so nothing is sealed to it.
      await cancelPrivateRegistration(pending).catch(() => {});
      await deletePushDevice(saved.id).catch(() => {});
      throw e;
    }
    await recordPushRegistration(saved.id, context);
    return;
  }
}

/**
 * Stores this device's token on the server, bound to the app's connection when it has one,
 * and hands the registration ID back so the app can remove it on disconnect. From 0.15.0,
 * where the server offers private push, the app's own enrollment replaces the plain one.
 */
export async function saveEnrollment(
  registration: PushRegistration & { token: string },
  context: NativeConnectionContext | null,
): Promise<void> {
  if (context && (await privatePushOffered())) {
    await savePrivateEnrollment(context);
    return;
  }
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
