<script lang="ts">
  import { Icon, CategoryGroup, ListRow } from '$lib/components/ui';
  import { Folder, Cloud, Paperclip } from '@lucide/svelte';
  import type { WorkspacePlace } from '$lib/api';
  import TaskReachIcon from './TaskReachIcon.svelte';
  import { reachesTasks } from './reach';
  let {
    places,
    botName,
    selected,
    href,
    onNavigate,
  }: {
    places: WorkspacePlace[];
    botName: string;
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
        <ListRow
          href={href(place.id)}
          active={selected === place.id}
          title={place.label}
          aria-describedby={reachesTasks(place.reach.private)
            ? `task-reach-${place.id}`
            : undefined}
          onclick={(event) => onNavigate(event, place.id)}
        >
          {#snippet icon()}
            <span class="place-icon"
              >{#if place.kind === 'talk'}<Icon icon={Paperclip} size="sm" />{:else}<Icon
                  icon={group.icon}
                  size="sm"
                />{/if}</span
            >
          {/snippet}
          {place.label}
          {#snippet badge()}
            <TaskReachIcon access={place.reach.private} {botName} id={`task-reach-${place.id}`} />
          {/snippet}
        </ListRow>
      {/each}
    </CategoryGroup>
  {/if}
{/each}

<style>
  .place-icon {
    display: inline-flex;
    color: var(--accent-amber);
  }
</style>
