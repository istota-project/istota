<script lang="ts">
  import type { WorkspacePlace } from '$lib/api';
  import ReachBadge from './ReachBadge.svelte';
  let { place, notes }: { place: WorkspacePlace; notes: string[] } = $props();
</script>

<details class="reach-panel">
  <summary>How Istota reaches your files</summary>
  <div class="reach-body">
    {#if place.kind !== 'nextcloud'}
      {#each notes as note}<p>{note}</p>{/each}
      <dl>
        <dt>Private room</dt>
        <dd><ReachBadge access={place.reach.private} /></dd>
        <dt>Your turn in a shared room</dt>
        <dd><ReachBadge access={place.reach.shared_member} /></dd>
        <dt>Guest turn or unasked task</dt>
        <dd><ReachBadge access={place.reach.restricted} /></dd>
      </dl>
    {/if}
    {#if place.reach.note}<p>{place.reach.note}</p>{/if}
  </div>
</details>

<style>
  .reach-panel {
    border-bottom: 1px solid var(--border-subtle);
    padding: var(--space-3);
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  summary {
    cursor: pointer;
    width: fit-content;
  }
  .reach-body {
    padding-top: var(--space-2);
  }
  p {
    margin: var(--space-2) 0;
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
