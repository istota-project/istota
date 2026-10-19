<!--
  The load, failure and empty states around something fetched, in one place.
  `scope="pane"` is the whole-pane chain `web/AGENTS.md` describes: `PageLoading`
  while loading, `.center-msg error` for the failure (its exclusive twin, so the
  same box), the empty snippet at the top of the pane. `scope="card"` is the same
  chain inside a card: a caption line and a `.banner error`. The page still
  computes the error string; this only decides where it goes.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { notifyError } from '$lib/stores/notices';
  import Button from './Button.svelte';
  import PageLoading from './PageLoading.svelte';

  interface Props {
    loading: boolean;
    error?: string | null;
    /** Loaded with nothing to show: renders `emptyState` in place of the children. */
    empty?: boolean;
    /**
     * Data is already on screen. A refresh then changes nothing while it loads,
     * and its failure is reported out of band (`notifyError`) rather than
     * replacing what the reader is looking at.
     */
    hasData?: boolean;
    loadingLabel?: string;
    /** Draws a Retry button beside the failure. */
    onRetry?: () => void;
    retryLabel?: string;
    /** Usually an `EmptyState`; a page with filters passes the filtered wording. */
    emptyState?: Snippet;
    /**
     * Chrome above the content (a toolbar, a page header): held back while the
     * first load runs and when it fails, so the failure has the whole pane, and
     * shown with the empty state as well as the data.
     */
    header?: Snippet;
    scope?: 'pane' | 'card';
    children: Snippet;
  }

  let {
    loading,
    error = null,
    empty = false,
    hasData = false,
    loadingLabel = 'Loading…',
    onRetry,
    retryLabel = 'Retry',
    emptyState,
    header,
    scope = 'pane',
    children,
  }: Props = $props();

  $effect(() => {
    if (error && hasData) notifyError(error);
  });
</script>

{#snippet content()}
  {#if header}{@render header()}{/if}
  {#if empty}
    {@render emptyState?.()}
  {:else}
    {@render children()}
  {/if}
{/snippet}

{#if scope === 'card'}
  {#if loading && !hasData}
    <p class="caption">{loadingLabel}</p>
  {:else if error && !hasData}
    <div class="banner error" role="alert">
      {error}
      {#if onRetry}<Button size="sm" variant="secondary" onclick={onRetry}>{retryLabel}</Button
        >{/if}
    </div>
  {:else}
    {@render content()}
  {/if}
{:else if loading && !hasData}
  <PageLoading label={loadingLabel} />
{:else if error && !hasData}
  <div class="center-msg error">
    {#if onRetry}
      <p>{error}</p>
      <Button variant="secondary" onclick={onRetry}>{retryLabel}</Button>
    {:else}
      {error}
    {/if}
  </div>
{:else}
  {@render content()}
{/if}
