<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    title: string;
    description?: string;
    loading?: boolean;
    error?: string;
    info?: string;
    headerActions?: Snippet;
    children: Snippet;
  }

  let {
    title,
    description,
    loading = false,
    error = '',
    info = '',
    headerActions,
    children,
  }: Props = $props();
</script>

<div class="settings">
  <header class="settings-header">
    <div>
      <h1>{title}</h1>
      {#if description}<p class="hint">{description}</p>{/if}
    </div>
    {#if headerActions}
      <div class="header-actions">{@render headerActions()}</div>
    {/if}
  </header>

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
