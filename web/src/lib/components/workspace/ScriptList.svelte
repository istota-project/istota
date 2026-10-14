<script lang="ts">
  import { Button, CategoryGroup } from '$lib/components/ui';
  import { FileCode, TriangleAlert } from '@lucide/svelte';
  import type { WorkspaceScriptsIndex } from '$lib/api';
  import type { IndexView } from './indexViews';

  let {
    index,
    loading,
    error,
    active,
    selected,
    viewHref,
    onNavigateView,
    onRetry,
  }: {
    index: WorkspaceScriptsIndex | null;
    loading: boolean;
    error: string;
    active: boolean;
    selected?: string;
    viewHref: (view: IndexView, script?: string) => string;
    onNavigateView: (event: MouseEvent, view: IndexView, script?: string) => void;
    onRetry: () => void;
  } = $props();

  const scripts = $derived(index?.files.filter((file) => file.kind === 'script') ?? []);
</script>

<section aria-label="Scripts">
  <CategoryGroup
    label="Scripts"
    collapsible
    defaultOpen={false}
    storageKey="workspace:section:scripts"
  >
    {#if loading}
      <p class="caption">Loading scripts…</p>
    {:else if error}
      <p class="form-error" role="alert">{error}</p>
      <Button variant="secondary" size="sm" onclick={onRetry}>Retry scripts</Button>
    {:else}
      <a
        class="view-btn"
        class:active={active && !selected}
        href={viewHref('scripts')}
        aria-label="All scripts"
        aria-current={active && !selected ? 'page' : undefined}
        onclick={(event) => onNavigateView(event, 'scripts')}
      >
        <FileCode size={14} aria-hidden="true" />
        <span class="view-name">All scripts</span>
        <span class="caption script-count">{scripts.length}</span>
        {#if index?.broken.length}
          <span class="script-warning">
            <TriangleAlert size={14} role="img" aria-label="Broken script references" />
          </span>
        {/if}
      </a>
      {#each scripts as script, i (script.path)}
        <a
          class="view-btn"
          class:active={active && selected === script.path}
          href={viewHref('scripts', script.path)}
          aria-label={script.path}
          aria-describedby={`script-description-${i}`}
          aria-current={active && selected === script.path ? 'page' : undefined}
          onclick={(event) => onNavigateView(event, 'scripts', script.path)}
        >
          <span class="view-name script-label" title={script.path}>
            <span>{script.path}</span>
            <span
              class="caption"
              id={`script-description-${i}`}
              title={script.description ?? undefined}
            >
              {script.description || 'No description'}
            </span>
          </span>
        </a>
      {/each}
    {/if}
  </CategoryGroup>
</section>

<style>
  .script-label > span {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .script-count,
  .script-warning,
  .view-btn > :global(svg) {
    flex-shrink: 0;
  }
  .view-btn > :global(svg) {
    color: var(--accent-amber);
  }
  .script-warning {
    display: flex;
    color: var(--status-warn-fg);
  }
</style>
