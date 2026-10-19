import { shellAtLeast } from './native';

/**
 * The generic iOS shell's connection, as its own native code describes it.
 *
 * From 0.12.0 the app connects to a server the user chose, and every native
 * enrollment names that connection so stale work from an earlier one can be
 * refused. Older shells and ordinary browsers have no connection plugin and
 * every call here is a no-op returning null.
 */
export interface NativeConnectionContext {
  connectionBinding: string;
  generation: number;
  baseURL: string;
  state: 'selected' | 'active';
}
interface ConnectionPlugin {
  current(): Promise<NativeConnectionContext>;
  recordPushRegistration(input: {
    registrationId: number;
    connectionBinding: string;
    generation: number;
  }): Promise<void>;
}
function connectionPlugin(): ConnectionPlugin | null {
  if (!shellAtLeast('0.12.0') || typeof window === 'undefined') return null;
  return (
    (window as { Capacitor?: { Plugins?: { IstotaConnection?: ConnectionPlugin } } }).Capacitor
      ?.Plugins?.IstotaConnection ?? null
  );
}

/** Rejects when the page no longer belongs to the app's current connection. */
export async function connectionContext(): Promise<NativeConnectionContext | null> {
  return (await connectionPlugin()?.current()) ?? null;
}

/** Hands the server's registration ID to the app, so it can remove it on disconnect. */
export async function recordPushRegistration(
  registrationId: number,
  context: NativeConnectionContext,
): Promise<void> {
  await connectionPlugin()?.recordPushRegistration({
    registrationId,
    connectionBinding: context.connectionBinding,
    generation: context.generation,
  });
}
