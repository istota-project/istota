<script lang="ts">
  import { PageLoading } from '$lib/components/ui';
  import type { Snippet } from 'svelte';

  interface Props {
    /* Optional: a page whose section title already sits in the `ShellHeader`
       above (/settings, and any future top-level settings route) passes none,
       rather than repeating the same words two rows apart. */
    title?: string;
    description?: string;
    loading?: boolean;
    error?: string;
    info?: string;
    headerActions?: Snippet;
    children: Snippet;
  }

  let {
    title = '',
    description,
    loading = false,
    error = '',
    info = '',
    headerActions,
    children,
  }: Props = $props();
</script>

<div class="settings">
  <!-- Held back while loading so the pane shows nothing but the centered
       loading message, rather than centering it in the space left under this
       header. The banners below stay: they are feedback about the last action,
       and a reload should not swallow them. -->
  {#if !loading && (title || description || headerActions)}
    <header class="settings-header">
      <div>
        {#if title}<h1 class="title-page">{title}</h1>{/if}
        {#if description}<p class="description">{description}</p>{/if}
      </div>
      {#if headerActions}
        <div class="header-actions">{@render headerActions()}</div>
      {/if}
    </header>
  {/if}

  {#if error}
    <div class="banner error">{error}</div>
  {/if}
  {#if info}
    <div class="banner info">{info}</div>
  {/if}

  {#if loading}
    <PageLoading />
  {:else}
    {@render children()}
  {/if}
</div>
