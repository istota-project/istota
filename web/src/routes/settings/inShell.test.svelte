<script lang="ts">
  /* Test-only harness: a settings page inside `AppShell`, which is what scrolls.
     `show` mounts the page after the pane has an offset, as a section switch does. */
  import { AppShell } from '@minot/ui';
  import type { Component } from 'svelte';

  let {
    component: Page,
    pageProps = {},
    show = true,
  }: { component: Component<any>; pageProps?: Record<string, unknown>; show?: boolean } = $props();
</script>

<AppShell>
  {#snippet header()}<div></div>{/snippet}
  {#if show}<Page {...pageProps} />{/if}
</AppShell>
