/**
 * The shell's native actions (`IstotaActions`), as the web app sees them.
 *
 * The shell saves quick notes from native code with a per-device token, so a
 * note goes out with no web session at all. The web app's part is small: mint
 * the token and hand it over (`provisionActions`), and act on the "open this"
 * requests the native sheet leaves behind (`consumeActionOpen`). The token goes
 * one way only. `status` never returns it, the same rule `nativeLocation.ts`
 * keeps for the tracker's ingest token.
 *
 * Every export degrades to the empty answer off-shell, on a shell older than
 * 0.13.0, or when the bridge throws, so a shell that does not answer looks like
 * one without the plugin (rule 1 in `native.ts`).
 */

import { shellAtLeast } from './native';

export const SHELL_WITH_ACTIONS = '0.13.0';

export interface ActionsStatus {
  deviceId: string;
  /** Scopes of the token the shell holds; [] when it holds none. */
  scopes: string[];
  /** The last send got a 401 (or 403): the token needs replacing. */
  tokenRejected: boolean;
  pendingNotes: number;
  failedNotes: number;
}

export type ActionOpen =
  | { kind: 'notes-settings' }
  /** A path relative to My files: a saved note, or the folder it is in. */
  | { kind: 'files'; path: string };

interface ActionsPlugin {
  status(): Promise<ActionsStatus>;
  provision(opts: {
    token: string | null;
    scopes: string[];
    notesFolder: string | null;
  }): Promise<void>;
  forget(): Promise<void>;
  consumePendingOpen(): Promise<{ open: ActionOpen | null }>;
}

function actionsPlugin(): ActionsPlugin | null {
  if (!shellAtLeast(SHELL_WITH_ACTIONS) || typeof window === 'undefined') return null;
  return (
    (window as { Capacitor?: { Plugins?: { IstotaActions?: ActionsPlugin } } }).Capacitor?.Plugins
      ?.IstotaActions ?? null
  );
}

export function actionsAvailable(): boolean {
  return actionsPlugin() !== null;
}

export async function actionsStatus(): Promise<ActionsStatus | null> {
  const plugin = actionsPlugin();
  if (!plugin) return null;
  try {
    const status = await plugin.status();
    if (!status || typeof status.deviceId !== 'string') return null;
    return {
      deviceId: status.deviceId,
      scopes: Array.isArray(status.scopes) ? status.scopes : [],
      tokenRejected: status.tokenRejected === true,
      pendingNotes: Number(status.pendingNotes) || 0,
      failedNotes: Number(status.failedNotes) || 0,
    };
  } catch {
    console.warn('[istota] Could not read the native actions status.');
    return null;
  }
}

/**
 * Hand the shell its token. `token: null` keeps the token the shell already
 * holds and changes only the scopes and the folder label it shows.
 */
export async function provisionActions(
  token: string | null,
  scopes: string[],
  notesFolder: string | null,
): Promise<void> {
  try {
    await actionsPlugin()?.provision({ token, scopes, notesFolder });
  } catch {
    console.warn('[istota] Could not provision the native actions token.');
  }
}

/** Delete the shell's token. Its queue of unsent notes stays. */
export async function forgetActions(): Promise<void> {
  try {
    await actionsPlugin()?.forget();
  } catch {
    console.warn('[istota] Could not forget the native actions token.');
  }
}

export async function consumeActionOpen(): Promise<ActionOpen | null> {
  const plugin = actionsPlugin();
  if (!plugin) return null;
  try {
    return (await plugin.consumePendingOpen())?.open ?? null;
  } catch {
    console.warn('[istota] Could not read the pending native action.');
    return null;
  }
}

export function onActionOpen(handler: () => void): () => void {
  if (!actionsAvailable()) return () => {};
  // The native buffer owns the payload; the event only wakes its consumer.
  const listener = () => handler();
  window.addEventListener('istotaActionOpen', listener);
  return () => window.removeEventListener('istotaActionOpen', listener);
}
