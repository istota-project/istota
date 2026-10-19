<script lang="ts">
  import { CategoryGroup, ListRow } from '$lib/components/ui';
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
      <ListRow
        href={href(home.id)}
        active={current === 'home'}
        title={home.label}
        aria-describedby={reachesTasks(home.reach.private) ? `task-reach-${home.id}` : undefined}
        onclick={(event) => onNavigate(event, home.id)}
      >
        {#snippet icon()}<span class="entry-icon"><House size={14} /></span>{/snippet}
        {home.label}
        {#snippet badge()}
          <TaskReachIcon access={home.reach.private} botName={label} id={`task-reach-${home.id}`} />
        {/snippet}
      </ListRow>
    {/if}
    {#each ENTRIES.filter((entry) => views.includes(entry.view)) as entry (entry.view)}
      <ListRow
        href={viewHref(entry.view)}
        active={current === entry.view}
        aria-current={current === entry.view ? (viewOpen ? 'page' : 'location') : undefined}
        onclick={(event) => onNavigateView(event, entry.view)}
      >
        {#snippet icon()}<span class="entry-icon"><entry.icon size={14} /></span>{/snippet}
        {entry.label}
        {#snippet badge()}
          {#if warnings[entry.view]}
            <span class="view-warning">
              <TriangleAlert size={14} role="img" aria-label={warnings[entry.view]} />
            </span>
          {/if}
        {/snippet}
      </ListRow>
    {/each}
  </CategoryGroup>
</section>

<style>
  .entry-icon {
    display: inline-flex;
    color: var(--accent-amber);
  }
  .view-warning {
    display: flex;
    color: var(--status-warn-fg);
  }
</style>
