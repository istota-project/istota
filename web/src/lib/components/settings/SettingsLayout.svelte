<script lang="ts">
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
  {#if title || description || headerActions}
    <header class="settings-header">
      <div>
        {#if title}<h1>{title}</h1>{/if}
        {#if description}<p class="hint">{description}</p>{/if}
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
    <div class="placeholder">Loading…</div>
  {:else}
    {@render children()}
  {/if}
</div>
