<script lang="ts">
  import { MessageSquare, Users } from '@lucide/svelte';
  import { EmptyState } from '$lib/components/ui';
  import type { WorkspacePlace } from '$lib/api';
  import ReachBadge from './ReachBadge.svelte';

  let {
    label,
    caption,
    empty,
    places,
    href,
    onNavigate,
  }: {
    label: string;
    caption: string;
    empty: string;
    places: WorkspacePlace[];
    href: (place: string, path?: string) => string;
    onNavigate: (event: MouseEvent, place: string, path?: string) => void;
  } = $props();
</script>

<section class="index" aria-label={label}>
  {#if places.length}
    <p class="caption index-caption">{caption}</p>
    <ul>
      {#each places as place (place.id)}
        <li class="index-row">
          <a
            class="index-name"
            href={href(place.id)}
            onclick={(event) => onNavigate(event, place.id)}
            title={place.label}
          >
            {#if place.kind === 'group'}<Users size={14} aria-hidden="true" />{:else}<MessageSquare
                size={14}
                aria-hidden="true"
              />{/if}<span class="index-label">{place.label}</span>
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
  .index ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .index-caption {
    margin: 0;
    padding: var(--space-3);
  }
  .index-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
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
  .index-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
