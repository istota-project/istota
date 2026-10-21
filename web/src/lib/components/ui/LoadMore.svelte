<svelte:options runes={true} />

<!--
  The next page of a list that loads in pages: a secondary button under it, or
  with `auto` a sentinel that loads as it scrolls into view (the feed grid).
  The list's own state stays with the page; this only asks for more.
-->
<script lang="ts">
  import { getShellScrollRoot } from './AppShell.svelte';
  import Button from './Button.svelte';

  interface Props {
    /**
     * Loads the next page. With `auto`, resolving `false` says the load failed
     * and the sentinel should not ask again until it scrolls back into view,
     * so a failing page is not retried in a loop.
     */
    onload: () => unknown;
    loading?: boolean;
    hasMore: boolean;
    label?: string;
    /** Shown in place of the button once there is no more (why it stopped). */
    note?: string;
    error?: string | null;
    /** The button spans its container (a sidebar list). */
    fullWidth?: boolean;
    /** Load as the end of the list scrolls into view, with no button. */
    auto?: boolean;
  }

  let {
    onload,
    loading = false,
    hasMore,
    label = 'Load more',
    note,
    error = null,
    fullWidth = false,
    auto = false,
  }: Props = $props();

  const scrollRoot = getShellScrollRoot();
  let sentinel: HTMLDivElement | undefined = $state();

  $effect(() => {
    if (!auto || !hasMore || !sentinel) return;
    const target = sentinel;
    let pending = false;
    let disposed = false;
    const observer = new IntersectionObserver(
      async (entries) => {
        if (!entries[0]?.isIntersecting || pending || loading) return;
        pending = true;
        let result: unknown;
        try {
          result = await onload();
        } catch {
          result = false;
        } finally {
          pending = false;
        }
        // A short page can leave the sentinel in view across the whole load,
        // and an observer only reports a change; observing again re-reports it.
        if (result !== false && !disposed) {
          observer.unobserve(target);
          observer.observe(target);
        }
      },
      { root: scrollRoot?.() ?? null, rootMargin: '600px' },
    );
    observer.observe(target);
    return () => {
      disposed = true;
      observer.disconnect();
    };
  });
</script>

{#if error || hasMore || note}
  <div class="ui-load-more" class:full-width={fullWidth}>
    {#if error}<p class="form-error" role="alert">{error}</p>{/if}
    {#if auto}
      {#if hasMore}
        <div class="ui-load-more-sentinel" bind:this={sentinel}>
          {#if loading}<p class="caption">Loading more…</p>{/if}
        </div>
      {/if}
    {:else if hasMore}
      <Button
        variant="secondary"
        size="sm"
        {loading}
        loadingLabel="Loading…"
        onclick={() => onload()}>{label}</Button
      >
    {/if}
    {#if !hasMore && note}<p class="caption">{note}</p>{/if}
  </div>
{/if}

<style>
  .ui-load-more {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) 0;
  }

  .full-width {
    align-items: stretch;
  }

  .ui-load-more p {
    margin: 0;
    text-align: center;
  }

  .ui-load-more-sentinel {
    min-height: 1px;
    align-self: stretch;
  }
</style>
