<script lang="ts">
  import { Alert, Icon } from '@minot/ui';
  import { Bot } from '@lucide/svelte';

  let { caption, reachNote = null }: { caption: string; reachNote?: string | null } = $props();
</script>

<div class="index-caption">
  <p>{caption}</p>
  {#if reachNote}
    <Alert appearance="inline" variant="info"
      ><span class="hstack"><Icon icon={Bot} size="sm" />{reachNote}</span></Alert
    >
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
</style>
