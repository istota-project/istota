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
  openSettings(): Promise<void>;
  signOut(): Promise<void>;
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

/** Whether the app has native settings and a native sign-out for this page to hand over to. */
export function appConnectionAvailable(): boolean {
  return connectionPlugin() !== null;
}

/**
 * Whether a server route that redirects to another site, such as an OAuth connect, has to
 * open in the system browser. The 0.12.0 app stops a redirect off the site that the user
 * did not tap and loses the page, but opens a tapped new-window link in its browser view.
 * That view has its own cookies, so the user may have to sign in to Istota there.
 */
export function offSiteRoutesOpenInBrowser(): boolean {
  return appConnectionAvailable();
}

/** Opens the app's own settings. False when there is no app to open them in. */
export async function openAppSettings(): Promise<boolean> {
  const plugin = connectionPlugin();
  if (!plugin) return false;
  await plugin.openSettings();
  return true;
}

export type AppSignOut = 'signed-out' | 'cancelled' | 'unavailable';

/**
 * Hands sign-out to the app, which asks natively and then stops tracking, removes the
 * notification enrollment and clears this page's website data before anyone signs in again.
 *
 * 'cancelled' is the user saying no, or a question already on screen; neither is an error.
 * The page is retired by the app after 'signed-out', so a caller has nothing left to do.
 */
export async function signOutOfApp(): Promise<AppSignOut> {
  const plugin = connectionPlugin();
  if (!plugin) return 'unavailable';
  try {
    await plugin.signOut();
    return 'signed-out';
  } catch (e) {
    const code = (e as { code?: unknown } | null)?.code;
    if (code === 'USER_CANCELLED' || code === 'SIGN_OUT_PENDING') return 'cancelled';
    throw e;
  }
}
