<script lang="ts">
  import { Bot } from '@lucide/svelte';
  import type { WorkspacePlace } from '$lib/api';
  import ReachBadge from './ReachBadge.svelte';
  let { place, notes }: { place: WorkspacePlace; notes: string[] } = $props();
</script>

<!-- The Nextcloud view is the user's own, so its note is the whole story and stays visible. -->
{#if place.kind === 'nextcloud'}
  {#if place.reach.note}<p class="reach-notice">{place.reach.note}</p>{/if}
{:else}
  <details class="reach-panel">
    <summary>How Istota reaches your files</summary>
    <div class="reach-body">
      <p class="reach-legend">
        <Bot size={14} role="img" aria-label="The robot icon" /> in the sidebar means a task in your private
        room can reach that place, or for a room folder, a task in that room. Places without it are visible
        to you only.
      </p>
      {#each notes as note}<p>{note}</p>{/each}
      <dl>
        <dt>Private room</dt>
        <dd><ReachBadge access={place.reach.private} /></dd>
        <dt>Your turn in a shared room</dt>
        <dd><ReachBadge access={place.reach.shared_member} /></dd>
        <dt>Guest turn or unasked task</dt>
        <dd><ReachBadge access={place.reach.restricted} /></dd>
      </dl>
      {#if place.reach.note}<p>{place.reach.note}</p>{/if}
    </div>
  </details>
{/if}

<style>
  .reach-panel,
  .reach-notice {
    border-bottom: 1px solid var(--border-subtle);
    padding: var(--space-3);
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .reach-notice {
    margin: 0;
  }
  summary {
    cursor: pointer;
    width: fit-content;
  }
  .reach-body {
    padding-top: var(--space-2);
  }
  .reach-body p {
    margin: var(--space-2) 0;
  }
  .reach-legend :global(svg) {
    vertical-align: text-bottom;
  }
  dl {
    display: grid;
    grid-template-columns: minmax(0, max-content) auto;
    align-items: center;
    gap: var(--space-2) var(--space-4);
    margin: var(--space-3) 0;
  }
  dd {
    margin: 0;
  }
</style>
