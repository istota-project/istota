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
interface PushPlugin {
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
