<script lang="ts">
  import LoadState from './LoadState.svelte';

  let {
    loading,
    error = null,
    empty = false,
    hasData = false,
    scope = 'pane',
    loadingLabel,
    onRetry,
    withEmpty = true,
    withHeader = false,
    announce,
  }: {
    loading: boolean;
    error?: string | null;
    empty?: boolean;
    hasData?: boolean;
    scope?: 'pane' | 'card';
    loadingLabel?: string;
    onRetry?: () => void;
    withEmpty?: boolean;
    withHeader?: boolean;
    announce?: boolean;
  } = $props();
</script>

{#snippet emptyNote()}<p data-testid="empty">Nothing here.</p>{/snippet}
{#snippet bar()}<div data-testid="header">Toolbar</div>{/snippet}

<LoadState
  {loading}
  {error}
  {empty}
  {hasData}
  {scope}
  {loadingLabel}
  {onRetry}
  {announce}
  emptyState={withEmpty ? emptyNote : undefined}
  header={withHeader ? bar : undefined}
>
  <ul data-testid="data"><li>Row</li></ul>
</LoadState>
