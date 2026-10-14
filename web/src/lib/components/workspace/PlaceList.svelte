<script lang="ts">
  import { CategoryGroup } from '$lib/components/ui';
  import { Folder, Cloud, Paperclip } from '@lucide/svelte';
  import type { WorkspacePlace } from '$lib/api';
  import TaskReachIcon from './TaskReachIcon.svelte';
  import { reachesTasks } from './reach';
  let {
    places,
    selected,
    href,
    onNavigate,
  }: {
    places: WorkspacePlace[];
    selected: string;
    href: (place: string, path?: string) => string;
    onNavigate: (event: MouseEvent, place: string, path?: string) => void;
  } = $props();
  // My files, rooms and groups live in the bot section and its index views.
  const groups = [
    { label: 'Linked folders', kinds: ['linked'], icon: Folder },
    { label: 'Connections', kinds: ['drive', 'nextcloud', 'talk'], icon: Cloud },
  ];
</script>

{#each groups as group}
  {@const items = places.filter((place) => group.kinds.includes(place.kind))}
  {#if items.length}
    <CategoryGroup
      label={group.label}
      collapsible
      defaultOpen={false}
      storageKey={`workspace:section:${group.kinds[0]}`}
    >
      {#each items as place (place.id)}
        <a
          class="view-btn"
          class:active={selected === place.id}
          href={href(place.id)}
          aria-current={selected === place.id ? 'page' : undefined}
          aria-describedby={reachesTasks(place.reach.private)
            ? `task-reach-${place.id}`
            : undefined}
          onclick={(event) => onNavigate(event, place.id)}
        >
          {#if place.kind === 'talk'}<Paperclip size={14} />{:else}<group.icon size={14} />{/if}
          <span class="view-name" title={place.label}>{place.label}</span>
          <TaskReachIcon access={place.reach.private} id={`task-reach-${place.id}`} />
        </a>
      {/each}
    </CategoryGroup>
  {/if}
{/each}

<style>
  .view-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .view-btn :global(svg) {
    flex-shrink: 0;
  }
</style>
