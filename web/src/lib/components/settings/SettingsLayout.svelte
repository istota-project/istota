<script lang="ts">
  import { Alert, LoadState, PageHeader } from '@minot/ui';
  import type { Snippet } from 'svelte';

  interface Props {
    /* Optional: a page whose section title already sits in the `ShellHeader`
       above (/settings, and any future top-level settings route) passes none,
       rather than repeating the same words two rows apart. */
    title?: string;
    description?: string;
    loading?: boolean;
    /** Feedback about the last action, above content that is still there. */
    error?: string;
    /** The page's own data failed to load, so there is nothing to show: the
     *  failure takes the whole pane, with Retry when `onRetry` is given. */
    loadError?: string;
    onRetry?: () => void;
    info?: string;
    headerActions?: Snippet;
    children: Snippet;
  }

  let {
    title = '',
    description,
    loading = false,
    error = '',
    loadError = '',
    onRetry,
    info = '',
    headerActions,
    children,
  }: Props = $props();
</script>

<div class="settings">
  <!-- Held back while loading, and on a failed load, so the pane shows nothing
       but the centered message, rather than centering it in the space left under this
       header. The banners below stay: they are feedback about the last action,
       and a reload should not swallow them. -->
  {#if !loading && !loadError && (title || description || headerActions)}
    <PageHeader title={title || undefined} {description} align="start" actions={headerActions} />
  {/if}

  {#if error}
    <Alert appearance="inline" variant="danger" role="alert">{error}</Alert>
  {/if}
  {#if info}
    <Alert appearance="inline" variant="info" role="status">{info}</Alert>
  {/if}

  <LoadState {loading} error={loadError} {onRetry}>
    {@render children()}
  </LoadState>
</div>
