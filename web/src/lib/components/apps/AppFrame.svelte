<script lang="ts" module>
  /** The frame's whole sandbox attribute. Adding a flag to it is a spec change. */
  export const FRAME_SANDBOX = 'allow-scripts';
</script>

<script lang="ts">
  /* One installed app version in its sandboxed frame, and the bridge to it.

     The frame is `sandbox="allow-scripts"`, exactly: never `allow-same-origin`,
     which would let the app's code run on Istota's origin with the user's
     session, and none of the popup, download or top-navigation flags. The
     element is built here rather than in markup so the sandbox attribute and
     the load watch are both in place before `src` is set; the first `load` is
     then the installed document's, and any later one is a navigation.

     Reload is the parent's: it remounts this component, which builds a new
     frame and a new bridge, so no counter carries over. */
  import { onDestroy, onMount } from 'svelte';
  import { copyText } from '@minot/ui/clipboard';
  import { get } from 'svelte/store';
  import { callApp, reportAppError, type BridgeErrorCode } from '$lib/apps/api';
  import { createBridge } from '$lib/apps/bridge';
  import { currentEnv, frameUrl, watchEnv, type AppEnv } from '$lib/apps/env';
  import { watchFrame } from '$lib/apps/frameWatch';
  import { online } from '$lib/stores/connectivity';

  interface Props {
    appId: string;
    version: number;
    /** The app's name, for the frame's accessible title. */
    name: string;
    /** The frame loaded something other than its document; it has been removed. */
    onNavigated?: () => void;
    onCallFailed?: (code: BridgeErrorCode) => void;
    onCallThrew?: (error: unknown) => void;
  }

  let { appId, version, name, onNavigated, onCallFailed, onCallThrew }: Props = $props();

  let host: HTMLDivElement;
  let cleanup: (() => void) | null = null;

  onMount(() => {
    // The identity of the frame is fixed for this mount; a new version is a remount.
    const id = appId;
    const ver = version;
    let env: AppEnv = currentEnv();

    const frame = document.createElement('iframe');
    frame.setAttribute('sandbox', FRAME_SANDBOX);
    frame.setAttribute('data-istota-app-frame', '');
    frame.setAttribute('title', name);
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.className = 'app-frame';

    const watch = watchFrame(frame, () => {
      bridge.dispose();
      frame.remove();
      void reportAppError(id, ver, {
        kind: 'navigated',
        message: 'The app loaded a page other than its own.',
      }).catch(() => {});
      onNavigated?.();
    });

    const bridge = createBridge({
      frameWindow: () => frame.contentWindow,
      navigations: () => watch.navigations,
      online: () => get(online),
      env: () => env,
      call: (request) => callApp(id, ver, request),
      copy: (text) => copyText(text),
      report: (report) => reportAppError(id, ver, report),
      onCallFailed: (code) => onCallFailed?.(code),
      onCallThrew: (error) => onCallThrew?.(error),
    });
    const detach = bridge.attach(window);
    const stopEnv = watchEnv((next) => {
      env = next;
      bridge.postEnv(next);
    });

    frame.src = frameUrl(id, ver, env);
    host.append(frame);

    cleanup = () => {
      stopEnv();
      detach();
      bridge.dispose();
      watch.dispose();
      frame.remove();
    };
  });

  onDestroy(() => cleanup?.());
</script>

<div class="app-frame-host" bind:this={host}></div>

<style>
  .app-frame-host {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .app-frame-host :global(.app-frame) {
    flex: 1;
    min-height: 0;
    width: 100%;
    border: 0;
    display: block;
    background: var(--surface-base);
  }
</style>
