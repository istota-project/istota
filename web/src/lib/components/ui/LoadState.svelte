<!--
  The load, failure and empty states around something fetched, in one place.
  `scope="pane"` is the whole-pane chain `web/AGENTS.md` describes: `PageLoading`
  while loading, `.center-msg error` for the failure (its exclusive twin, so the
  same box), the empty snippet at the top of the pane. `scope="card"` is the same
  chain inside a card: a caption line and an inline danger `Alert`. The page still
  computes the error string; this only decides where it goes.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { notifyError } from '$lib/stores/notices';
  import Alert from './Alert.svelte';
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
    /**
     * A card failure is a `role="alert"` live region. Pass `false` on every card
     * but one when several show the failure of one request, so it is read once.
     */
    announce?: boolean;
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
    announce = true,
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

{#snippet retryAction()}
  <Button size="sm" variant="secondary" onclick={onRetry}>{retryLabel}</Button>
{/snippet}

{#if scope === 'card'}
  {#if loading && !hasData}
    <p class="caption">{loadingLabel}</p>
  {:else if error && !hasData}
    <Alert
      appearance="inline"
      variant="danger"
      role={announce ? 'alert' : undefined}
      actions={onRetry ? retryAction : undefined}
    >
      {error}
    </Alert>
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
