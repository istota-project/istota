import {
  getMobilePushCapabilities,
  getPushSenderKey,
  listPushDevices,
  registerPushDevice,
  type PrivatePushEnrollment,
  type PushRelayRoute,
} from '$lib/api';
import { shellVersion } from './native';
import {
  cancelPrivateRegistration,
  commitPrivateRegistration,
  preparePrivateRegistration,
  privatePushAvailable,
  pushPermissions,
  relayPushAvailable,
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

type PrivateDelivery = 'direct' | 'relay';

const UNUSABLE = 'The app returned an unusable private enrollment.';
const RELAY_NEEDS_IOS_17 = 'Private notifications on this server need iOS 17 or later.';
const RELAY_NEEDS_UPDATE = 'Update the Istota app to get notifications from this server.';

/**
 * How the server offers private push to this app, if it does. Discovery keeps `delivery` at
 * `unavailable` under a relay so 0.12 to 0.14 shells still parse it; only a shell whose prepare
 * enrolls with the relay (0.16.0) takes that offer. A discovery read that fails counts as no
 * offer: a plain registration on a private installation gets the fixed alert, never text.
 */
async function privatePushOffer(): Promise<PrivateDelivery | null> {
  if (!privatePushAvailable()) return null;
  try {
    const push = await getMobilePushCapabilities();
    if (!(push.private?.versions ?? []).includes(1)) return null;
    if (push.delivery === 'direct') return 'direct';
    if (relayPushAvailable() && (push.relay?.versions ?? []).includes(1)) return 'relay';
    return null;
  } catch {
    return null;
  }
}

/** The route as the app returned it, checked for shape only; the server validates the rest. */
function relayRoute(prepared: PreparedPrivateRegistration): PushRelayRoute {
  const relay = prepared.relay;
  if (
    !relay ||
    typeof relay.route_id !== 'string' ||
    typeof relay.send_capability !== 'string' ||
    typeof relay.lease_expires_at !== 'number' ||
    !Number.isSafeInteger(relay.lease_expires_at)
  ) {
    throw new Error(UNUSABLE);
  }
  return {
    route_id: relay.route_id,
    send_capability: relay.send_capability,
    lease_expires_at: relay.lease_expires_at,
  };
}

function privateField(
  prepared: PreparedPrivateRegistration,
  senderKeyId: string,
  delivery: PrivateDelivery,
): PrivatePushEnrollment {
  if (prepared.content === 'generic') {
    return { version: 1, content: 'generic', generation: prepared.generation };
  }
  if (
    prepared.content !== 'encrypted' ||
    !prepared.encryptionPublicKey ||
    !prepared.encryptionKeyId
  ) {
    throw new Error(UNUSABLE);
  }
  return {
    version: 1,
    content: 'encrypted',
    encryption_public_key: prepared.encryptionPublicKey,
    encryption_key_id: prepared.encryptionKeyId,
    sender_key_id: senderKeyId,
    generation: prepared.generation,
    ...(delivery === 'relay' ? { relay: relayRoute(prepared) } : {}),
  };
}

function conflict(e: unknown, message: string): boolean {
  return (e as { status?: unknown } | null)?.status === 409 && (e as Error).message === message;
}

/**
 * Sender key, native prepare, the registration with `private`, native commit. Anything that
 * fails after prepare cancels it, and a key rotated meanwhile restarts the flow once.
 */
async function savePrivateEnrollment(
  context: NativeConnectionContext,
  delivery: PrivateDelivery,
): Promise<void> {
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
        private: privateField(prepared, sender.key_id, delivery),
      });
    } catch (e) {
      await cancelPrivateRegistration(pending).catch(() => {});
      if (attempt === 0 && conflict(e, 'sender key changed')) continue;
      // iOS 15 and 16 have no HPKE, and a relay forwards only sealed envelopes.
      if (
        delivery === 'relay' &&
        prepared.content === 'generic' &&
        conflict(e, 'private push not offered')
      ) {
        throw new Error(RELAY_NEEDS_IOS_17);
      }
      throw e;
    }
    try {
      await commitPrivateRegistration({ ...pending, deviceId: saved.id });
    } catch (e) {
      await cancelPrivateRegistration(pending).catch(() => {});
      // Under a relay the cancel revokes the route the row now names, so the row goes to
      // repair at its next send; a plain registration would only be refused.
      if (delivery === 'relay') throw e;
      // The server holds a key the app no longer has. Re-register without it rather than
      // deleting: the upsert reused the row id, so a DELETE would remove the working row too.
      const plain = await registerPushDevice({
        token: prepared.token,
        environment: prepared.environment,
        platform: 'ios',
        app_version: shellVersion() ?? '',
        connection_binding: binding,
        connection_generation: context.generation,
      }).catch(() => null);
      if (plain) await recordPushRegistration(plain.id, context).catch(() => {});
      throw e;
    }
    await recordPushRegistration(saved.id, context);
    return;
  }
}

/**
 * Stores this device's token on the server, bound to the app's connection when it has one,
 * and hands the registration ID back so the app can remove it on disconnect. From 0.15.0,
 * where the server offers private push, the app's own enrollment replaces the plain one; from
 * 0.16.0 that includes a relay installation, where the registration carries the relay route.
 */
export async function saveEnrollment(
  registration: PushRegistration & { token: string },
  context: NativeConnectionContext | null,
): Promise<void> {
  const delivery = context ? await privatePushOffer() : null;
  if (context && delivery) {
    await savePrivateEnrollment(context, delivery);
    return;
  }
  let saved: { id: number };
  try {
    saved = await registerPushDevice({
      token: registration.token,
      environment: registration.environment,
      platform: 'ios',
      app_version: shellVersion() ?? '',
      ...(context
        ? {
            connection_binding: context.connectionBinding,
            connection_generation: context.generation,
          }
        : {}),
    });
  } catch (e) {
    // Only a relay installation refuses a plain registration, and only an app that cannot
    // enroll with its relay sends one there.
    if (conflict(e, 'private push not offered')) throw new Error(RELAY_NEEDS_UPDATE);
    throw e;
  }
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
