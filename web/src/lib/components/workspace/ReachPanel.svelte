<script lang="ts">
  import { Disclosure } from '$lib/components/ui';
  import { Bot } from '@lucide/svelte';
  import type { WorkspacePlace } from '$lib/api';
  import ReachBadge from './ReachBadge.svelte';
  let { place, notes, botName }: { place: WorkspacePlace; notes: string[]; botName: string } =
    $props();
</script>

<!-- The Nextcloud view is the user's own, so its note is the whole story and stays visible. -->
{#if place.kind === 'nextcloud'}
  {#if place.reach.note}<p class="reach-notice">{place.reach.note}</p>{/if}
{:else}
  <div class="reach-panel">
    <Disclosure summary="How {botName} reaches your files">
      <div class="reach-body">
        <p class="reach-legend">
          <Bot size={14} role="img" aria-label="The robot icon" /> in the sidebar means your tasks can
          reach that place, in any of your rooms. A room folder is reached only in that room, and a group
          folder in a shared room only when everyone there is in the group. Places without it are visible
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
    </Disclosure>
  </div>
{/if}

<style>
  .reach-panel,
  .reach-notice {
    border-bottom: 1px solid var(--border-subtle);
    padding: var(--space-3);
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
  .reach-notice {
    margin: 0;
  }
  .reach-body p {
    margin: var(--space-2) 0;
  }
  .reach-legend :global(svg) {
    vertical-align: text-bottom;
    color: var(--accent-blue);
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
