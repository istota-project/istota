<!--
  The page the browser service captures for a location share's PNG (Talk and
  WhatsApp). It asks the server nothing: the service calls
  `window.renderSnapshot(payload)`, the page draws exactly that payload with
  the same `ShareMap` the web chat card uses, and the promise settles once the
  map is idle or has failed. Loaded alone it is an empty frame.

  The root layout renders this route bare, as it does `/setup`: the capture
  context carries no cookie, so there is no identity to load.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import ShareMap from '$lib/components/location/ShareMap.svelte';
  import { applyTheme } from '$lib/stores/theme';
  import {
    SNAPSHOT_HEIGHT,
    SNAPSHOT_WIDTH,
    parseSnapshotPayload,
    type SnapshotPayload,
    type SnapshotResult,
  } from '$lib/location/snapshot';

  type SnapshotWindow = Window & {
    renderSnapshot?: (payload: unknown) => Promise<SnapshotResult>;
  };

  let payload = $state<SnapshotPayload | null>(null);
  let settle: ((result: SnapshotResult) => void) | null = null;

  function onReady(result: SnapshotResult) {
    settle?.(result);
    settle = null;
  }

  onMount(() => {
    // ShareMap's footer reads the app's theme tokens, so the page itself has
    // to be light, not only the basemap. Not persisted: this is no preference.
    applyTheme('light');

    let used = false;
    const target = window as SnapshotWindow;
    target.renderSnapshot = (raw: unknown) => {
      // One render per page load: the service opens a fresh context for each.
      if (used) return Promise.resolve({ ok: false, reason: 'already_rendered' });
      const parsed = parseSnapshotPayload(raw);
      if (!parsed) return Promise.resolve({ ok: false, reason: 'bad_payload' });
      used = true;
      return new Promise<SnapshotResult>((resolve) => {
        settle = resolve;
        payload = parsed;
      });
    };
    return () => {
      delete target.renderSnapshot;
      settle = null;
    };
  });
</script>

<div
  class="snapshot-frame"
  data-snapshot-frame
  style:width="{SNAPSHOT_WIDTH}px"
  style:height="{SNAPSHOT_HEIGHT}px"
>
  {#if payload}
    <ShareMap
      observation={payload.observation}
      basemap={payload.basemap}
      ownerLabel={payload.ownerLabel}
      interactive={false}
      theme="light"
      width="100%"
      height="100%"
      {onReady}
    />
  {/if}
</div>

<style>
  .snapshot-frame {
    overflow: hidden;
    background: var(--surface-card);
  }
</style>
