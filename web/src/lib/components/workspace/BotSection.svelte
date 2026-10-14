<script lang="ts">
  import { CategoryGroup } from '$lib/components/ui';
  import { BookOpen, House, MessageSquare, SlidersHorizontal, Users } from '@lucide/svelte';
  import type { WorkspacePlace } from '$lib/api';
  import TaskReachIcon from './TaskReachIcon.svelte';
  import { reachesTasks } from './reach';
  import type { IndexView } from './indexViews';

  let {
    label,
    home,
    views,
    current,
    href,
    viewHref,
    onNavigate,
    onNavigateView,
  }: {
    label: string;
    home: WorkspacePlace | undefined;
    views: IndexView[];
    /** The entry the main area is showing: a view, or `home` for My files. */
    current: IndexView | 'home' | null;
    href: (place: string, path?: string) => string;
    viewHref: (view: IndexView) => string;
    onNavigate: (event: MouseEvent, place: string, path?: string) => void;
    onNavigateView: (event: MouseEvent, view: IndexView) => void;
  } = $props();

  const ENTRIES: { view: IndexView; label: string; icon: typeof House }[] = [
    { view: 'rooms', label: 'Rooms', icon: MessageSquare },
    { view: 'groups', label: 'Groups', icon: Users },
    { view: 'config', label: 'Configuration', icon: SlidersHorizontal },
    { view: 'skills', label: 'Skill instructions', icon: BookOpen },
  ];
</script>

<section aria-label={label}>
  <CategoryGroup {label} collapsible defaultOpen storageKey="workspace:section:bot">
    {#if home}
      <a
        class="view-btn"
        class:active={current === 'home'}
        href={href(home.id)}
        aria-current={current === 'home' ? 'page' : undefined}
        aria-describedby={reachesTasks(home.reach.private) ? `task-reach-${home.id}` : undefined}
        onclick={(event) => onNavigate(event, home.id)}
      >
        <House size={14} />
        <span class="view-name" title={home.label}>{home.label}</span>
        <TaskReachIcon access={home.reach.private} id={`task-reach-${home.id}`} />
      </a>
    {/if}
    {#each ENTRIES.filter((entry) => views.includes(entry.view)) as entry (entry.view)}
      <a
        class="view-btn"
        class:active={current === entry.view}
        href={viewHref(entry.view)}
        aria-current={current === entry.view ? 'page' : undefined}
        onclick={(event) => onNavigateView(event, entry.view)}
      >
        <entry.icon size={14} />
        <span class="view-name">{entry.label}</span>
      </a>
    {/each}
  </CategoryGroup>
</section>

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
