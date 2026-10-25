/**
 * The environment an app frame is told about, built from the shell's stores.
 *
 * The starting values ride in the frame URL's fragment, which the server never
 * sees, so the kit (`src/apps/kit/istota-app.js`) applies theme and text scale
 * before first paint. Later changes go to the frame as `env` messages. The
 * names and values are the kit's: `theme=dark|light`, `scale=small|medium|large`,
 * `shell=web|ios`. `v` is the message protocol, which the kit carries as
 * `istota: 1`. The Mac app reports `web` (see the spec's Decisions).
 */
import { get } from 'svelte/store';
import { base } from '$app/paths';
import { isNativeShell, onKeyboardGeometry } from '$lib/platform/native';
import { online } from '$lib/stores/connectivity';
import { fontSize, type FontSize } from '$lib/stores/fontSize';
import { theme, type Theme } from '$lib/stores/theme';

export const PROTOCOL = 1;

export interface AppEnv {
  theme: Theme;
  scale: FontSize;
  shell: 'web' | 'ios';
  visibility: 'visible' | 'hidden';
  online: boolean;
  keyboard: boolean;
}

export function shellKind(): AppEnv['shell'] {
  return isNativeShell() ? 'ios' : 'web';
}

function visibility(): AppEnv['visibility'] {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
    ? 'hidden'
    : 'visible';
}

/** The environment now, read once from the stores. */
export function currentEnv(keyboard = false): AppEnv {
  return {
    theme: get(theme),
    scale: get(fontSize),
    shell: shellKind(),
    visibility: visibility(),
    online: get(online),
    keyboard,
  };
}

/** The fragment the kit reads before first paint: `theme=…&scale=…&shell=…&v=1`. */
export function frameFragment(env: Pick<AppEnv, 'theme' | 'scale' | 'shell'>): string {
  const params = new URLSearchParams({
    theme: env.theme,
    scale: env.scale,
    shell: env.shell,
    v: String(PROTOCOL),
  });
  return params.toString();
}

/** The frame route for an installed version, with the starting environment. */
export function frameUrl(
  appId: string,
  version: number,
  env: Pick<AppEnv, 'theme' | 'scale' | 'shell'>,
): string {
  return `${base}/api/apps/${encodeURIComponent(appId)}/v/${version}/frame#${frameFragment(env)}`;
}

/** The message that tells a running frame the environment changed. */
export function envMessage(env: AppEnv): { istota: number; kind: 'env'; env: AppEnv } {
  return { istota: PROTOCOL, kind: 'env', env: { ...env } };
}

/**
 * Call `onChange` with the whole environment whenever a part of it changes:
 * theme, text scale, connectivity, the document's visibility and, in the iOS
 * shell, the keyboard. Not called for the values it starts with. Returns the
 * cleanup.
 */
export function watchEnv(onChange: (env: AppEnv) => void): () => void {
  let env = currentEnv();
  let started = false;

  function update(patch: Partial<AppEnv>) {
    const next = { ...env, ...patch };
    const changed = (Object.keys(next) as (keyof AppEnv)[]).some((k) => next[k] !== env[k]);
    env = next;
    if (started && changed) onChange({ ...env });
  }

  const stops = [
    theme.subscribe((value) => update({ theme: value })),
    fontSize.subscribe((value) => update({ scale: value })),
    online.subscribe((value) => update({ online: value })),
  ];
  const onVisibility = () => update({ visibility: visibility() });
  document.addEventListener('visibilitychange', onVisibility);
  const stopKeyboard = isNativeShell()
    ? onKeyboardGeometry((height) => update({ keyboard: height > 0 }))
    : () => {};
  started = true;

  return () => {
    for (const stop of stops) stop();
    document.removeEventListener('visibilitychange', onVisibility);
    stopKeyboard();
  };
}
