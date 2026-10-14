<script lang="ts">
  import { CategoryGroup } from '$lib/components/ui';
  import {
    BookOpen,
    FileCode,
    FileText,
    House,
    MessageSquare,
    SlidersHorizontal,
    TriangleAlert,
    Users,
  } from '@lucide/svelte';
  import type { WorkspacePlace } from '$lib/api';
  import TaskReachIcon from './TaskReachIcon.svelte';
  import { reachesTasks } from './reach';
  import type { IndexView } from './indexViews';

  let {
    label,
    home,
    views,
    warnings = {},
    current,
    viewOpen,
    href,
    viewHref,
    onNavigate,
    onNavigateView,
  }: {
    label: string;
    home: WorkspacePlace | undefined;
    views: IndexView[];
    /** An entry whose index has a problem to look at, with the warning's accessible label. */
    warnings?: Partial<Record<IndexView, string>>;
    /** The entry the main area is showing: a view, or `home` for My files. */
    current: IndexView | 'home' | null;
    /** False while a room or group folder is open: its index entry is then an ancestor, not the page. */
    viewOpen: boolean;
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
    { view: 'scripts', label: 'Scripts', icon: FileCode },
    { view: 'prompts', label: 'Prompt files', icon: FileText },
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
        aria-current={current === entry.view ? (viewOpen ? 'page' : 'location') : undefined}
        onclick={(event) => onNavigateView(event, entry.view)}
      >
        <entry.icon size={14} />
        <span class="view-name">{entry.label}</span>
        {#if warnings[entry.view]}
          <span class="view-warning">
            <TriangleAlert size={14} role="img" aria-label={warnings[entry.view]} />
          </span>
        {/if}
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
  .view-btn > :global(svg) {
    color: var(--accent-amber);
  }
  .view-warning {
    display: flex;
    margin-left: auto;
    color: var(--status-warn-fg);
  }
</style>
