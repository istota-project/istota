import { shellAtLeast } from './native';

export type PushPermission = 'granted' | 'denied' | 'prompt';
export interface PushRegistration {
  permission: PushPermission;
  token: string | null;
  environment: 'production' | 'sandbox';
}
export interface PushOpen {
  kind: 'chat' | 'notification';
  room_token?: string;
  room_id?: number;
  link?: string;
  notification_id?: number;
}
/** What the app's prepare returns; `generic` on iOS 15 and 16, which have no HPKE. */
export interface PreparedPrivateRegistration {
  content: 'encrypted' | 'generic';
  encryptionPublicKey?: string;
  encryptionKeyId?: string;
  generation: string;
  token: string;
  environment: 'production' | 'sandbox';
  versions: number[];
}
interface PrivatePushMethods {
  preparePrivateRegistration(input: {
    binding: string;
    senderKey: string;
    senderKeyId: string;
  }): Promise<PreparedPrivateRegistration>;
  commitPrivateRegistration(input: {
    binding: string;
    generation: string;
    deviceId: number;
  }): Promise<object>;
  cancelPrivateRegistration(input: { binding: string; generation: string }): Promise<object>;
}
interface PushPlugin extends Partial<PrivatePushMethods> {
  permissions(): Promise<{ permission: PushPermission; registered: boolean }>;
  register(): Promise<PushRegistration>;
  setBadge(options: { count: number }): Promise<void>;
  consumePendingOpen(): Promise<{ open: PushOpen | null }>;
}
function pushPlugin(): PushPlugin | null {
  if (!shellAtLeast('0.11.0') || typeof window === 'undefined') return null;
  return (
    (window as { Capacitor?: { Plugins?: { IstotaPush?: PushPlugin } } }).Capacitor?.Plugins
      ?.IstotaPush ?? null
  );
}
export function pushAvailable(): boolean {
  return pushPlugin() !== null;
}

function privatePushPlugin(): PrivatePushMethods | null {
  if (!shellAtLeast('0.15.0')) return null;
  const plugin = pushPlugin();
  if (
    typeof plugin?.preparePrivateRegistration !== 'function' ||
    typeof plugin.commitPrivateRegistration !== 'function' ||
    typeof plugin.cancelPrivateRegistration !== 'function'
  ) {
    return null;
  }
  return plugin as PrivatePushMethods;
}
/** Whether this app can enroll for private push; the server's offer is a separate check. */
export function privatePushAvailable(): boolean {
  return privatePushPlugin() !== null;
}
export async function preparePrivateRegistration(input: {
  binding: string;
  senderKey: string;
  senderKeyId: string;
}): Promise<PreparedPrivateRegistration | null> {
  return (await privatePushPlugin()?.preparePrivateRegistration(input)) ?? null;
}
export async function commitPrivateRegistration(input: {
  binding: string;
  generation: string;
  deviceId: number;
}): Promise<void> {
  await privatePushPlugin()?.commitPrivateRegistration(input);
}
export async function cancelPrivateRegistration(input: {
  binding: string;
  generation: string;
}): Promise<void> {
  await privatePushPlugin()?.cancelPrivateRegistration(input);
}
export async function pushPermissions(): Promise<{
  permission: PushPermission;
  registered: boolean;
} | null> {
  return (await pushPlugin()?.permissions()) ?? null;
}
export async function registerForPush(): Promise<PushRegistration | null> {
  return (await pushPlugin()?.register()) ?? null;
}
export async function setBadge(count: number): Promise<void> {
  await pushPlugin()?.setBadge({ count });
}
export async function consumePendingOpen(): Promise<PushOpen | null> {
  return (await pushPlugin()?.consumePendingOpen())?.open ?? null;
}
export function onPushOpen(handler: () => void): () => void {
  if (!pushAvailable()) return () => {};
  // The native buffer owns the payload; events only wake its consumer.
  const listener = () => handler();
  window.addEventListener('istotaPushOpen', listener);
  return () => window.removeEventListener('istotaPushOpen', listener);
}
