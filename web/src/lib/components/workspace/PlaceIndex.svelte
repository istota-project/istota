<script lang="ts">
  import IndexCaption from './IndexCaption.svelte';
  import { MessageSquare, Users } from '@lucide/svelte';
  import { EmptyState } from '$lib/components/ui';
  import type { WorkspacePlace } from '$lib/api';
  import ReachBadge from './ReachBadge.svelte';

  let {
    label,
    caption,
    reachNote = null,
    empty,
    places,
    href,
    onNavigate,
  }: {
    label: string;
    caption: string;
    /** The bot icon's sentence for the whole list, when every entry shares one reach. */
    reachNote?: string | null;
    empty: string;
    places: WorkspacePlace[];
    href: (place: string, path?: string) => string;
    onNavigate: (event: MouseEvent, place: string, path?: string) => void;
  } = $props();
</script>

<section class="index" aria-label={label}>
  {#if places.length}
    <IndexCaption {caption} {reachNote} />
    <ul class="list-reset">
      {#each places as place (place.id)}
        <li class="record-row record-row--divided index-row">
          <a
            class="index-name"
            href={href(place.id)}
            onclick={(event) => onNavigate(event, place.id)}
            title={place.label}
          >
            {#if place.kind === 'group'}<Users size={14} aria-hidden="true" />{:else}<MessageSquare
                size={14}
                aria-hidden="true"
              />{/if}<span class="index-label truncate">{place.label}</span>
          </a>
          <ReachBadge access={place.reach.private} />
        </li>
      {/each}
    </ul>
  {:else}
    <EmptyState>{empty}</EmptyState>
  {/if}
</section>

<style>
  /* A record row (table.css), ruled like the file table beside it. */
  .index-row {
    align-items: center;
    justify-content: space-between;
  }
  .index-name {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
    color: var(--text-primary);
    text-decoration: none;
  }
  .index-name:hover .index-label {
    text-decoration: underline;
  }
  .index-name :global(svg) {
    flex-shrink: 0;
    color: var(--accent-amber);
  }
</style>
