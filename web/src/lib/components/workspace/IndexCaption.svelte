<script lang="ts">
  import { Bot } from '@lucide/svelte';

  let { caption, reachNote = null }: { caption: string; reachNote?: string | null } = $props();
</script>

<div class="index-caption">
  <p>{caption}</p>
  {#if reachNote}
    <p class="banner info reach-note"><Bot size={14} aria-hidden="true" />{reachNote}</p>
  {/if}
</div>

<style>
  .index-caption {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-3);
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
  .index-caption p {
    margin: 0;
  }
  .reach-note {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .reach-note :global(svg) {
    flex-shrink: 0;
  }
</style>
