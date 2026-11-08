<script lang="ts">
  import { tick, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import type { SearchHit, User } from '$lib/api';
  import { createSearch } from '$lib/stores/search';
  import { hrefFor } from '$lib/search/links';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import { History } from '@lucide/svelte';
  import { Button, Icon, Input, LoadMore, Modal, Select } from '@minot/ui';
  import SearchResultRow from './SearchResultRow.svelte';

  let {
    open = $bindable(false),
    controller = createSearch(),
    features = {},
  }: {
    open?: boolean;
    features?: Partial<User['features']>;
    controller?: ReturnType<typeof createSearch>;
  } = $props();
  const sourceOptions = $derived.by(() => {
    const options = [
      { value: '', label: 'All' },
      { value: 'chats', label: 'Chats' },
      { value: 'rooms', label: 'Rooms' },
      { value: 'memory', label: 'Memory' },
      { value: 'facts', label: 'Facts' },
    ];
    const modules = [
      ['briefings', 'Briefings'],
      ['feeds', 'Feeds'],
      ['health', 'Health'],
      ['location', 'Location'],
      ['money', 'Transactions'],
    ] as const;
    for (const [source, label] of modules) {
      if (features[source]) options.push({ value: source, label });
    }
    return options;
  });
  const uid = $props.id();
  const inputId = `${uid}-input`;
  const listId = `${uid}-results`;
  const groups = $derived(
    $controller.results.filter(
      (group) => group.results.length || group.error || $controller.selectedSource,
    ),
  );
  const actionable = (hit: SearchHit) => hit.link?.type === 'file' || hrefFor(hit.link) !== null;
  const options = $derived(groups.flatMap((group) => group.results).filter(actionable));
  const active = $derived(options[$controller.activeIndex]);
  const optionId = (hit: SearchHit) => `${uid}-${encodeURIComponent(hit.id)}`;
  const recents = $derived($controller.query.trim() ? [] : $controller.recentQueries);
  const recentId = (index: number) => `${uid}-recent-${index}`;
  const activeRecent = $derived(recents.length ? $controller.activeIndex : -1);
  const activeId = $derived(
    activeRecent >= 0 ? recentId(activeRecent) : active ? optionId(active) : undefined,
  );

  $effect(() => {
    if (!open) controller.close();
  });
  onDestroy(() => controller.close());
  $effect(() => {
    if (open && activeId) document.getElementById(activeId)?.scrollIntoView?.({ block: 'nearest' });
  });

  async function openHit(hit: SearchHit) {
    if (!actionable(hit)) return;
    const href = hrefFor(hit.link);
    const file = hit.link?.type === 'file' ? hit.link.path : null;
    controller.rememberQuery();
    open = false;
    await tick();
    if (href) await goto(href);
    else if (file) viewer.openFile(file);
  }

  async function useRecent(query: string) {
    controller.setQuery(query);
    await tick();
    document.getElementById(inputId)?.focus();
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.isComposing) return;
    const count = recents.length || options.length;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (count)
        controller.setActive(
          ($controller.activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + count) % count,
        );
    } else if (event.key === 'Enter' && activeRecent >= 0) {
      event.preventDefault();
      void useRecent(recents[activeRecent]);
    } else if (event.key === 'Enter' && active) {
      event.preventDefault();
      void openHit(active);
    }
  }

  async function selectSource(source: string | null) {
    controller.selectSource(source);
    await tick();
    document.getElementById(inputId)?.focus();
  }
</script>

<Modal
  bind:open
  title="Search"
  variant="palette"
  onOpenAutoFocus={(event) => {
    event.preventDefault();
    document.getElementById(inputId)?.focus();
  }}
>
  <div class="search-input-row control-row">
    <Input
      id={inputId}
      role="combobox"
      aria-label="Search everything"
      aria-expanded={true}
      aria-controls={listId}
      aria-activedescendant={activeId}
      aria-autocomplete="list"
      autocomplete="off"
      maxlength={200}
      placeholder="Search messages, notes, and more…"
      value={$controller.query}
      oninput={(event) => controller.setQuery(event.currentTarget.value)}
      onkeydown={handleKeydown}
    />
    <Select
      value={$controller.selectedSource ?? ''}
      options={sourceOptions}
      ariaLabel="Search sources"
      align="end"
      onValueChange={(value) => void selectSource(value || null)}
    />
  </div>
  <div class="search-results">
    {#if !$controller.query.trim()}
      {#if !recents.length}<p class="muted">Type at least 2 characters.</p>{/if}
    {:else if $controller.query.trim().length < 2}<p class="muted">
        Type at least 2 characters.
      </p>{/if}
    {#if $controller.loading}<p role="status" class="muted">Searching…</p>{/if}
    {#if $controller.error}<p role="status" class="muted">{$controller.error}</p>{/if}
    <div id={listId} role="listbox" aria-label="Search results" aria-busy={$controller.loading}>
      {#if recents.length}
        <div role="group" aria-labelledby={`${uid}-recent`} class="search-group">
          <h2 class="micro-label" id={`${uid}-recent`}>Recent searches</h2>
          {#each recents as query, index (query)}
            <button
              id={recentId(index)}
              type="button"
              role="option"
              aria-selected={index === activeRecent}
              tabindex="-1"
              class="recent-row"
              class:active={index === activeRecent}
              onclick={() => void useRecent(query)}
              ><Icon icon={History} /><span class="truncate">{query}</span></button
            >
          {/each}
        </div>
      {/if}
      {#each groups as group}
        <div role="group" aria-labelledby={`${uid}-group-${group.source}`} class="search-group">
          <h2 class="micro-label" id={`${uid}-group-${group.source}`}>{group.label}</h2>
          {#if group.relaxed}<p class="caption">No exact matches. Showing partial matches.</p>{/if}
          {#if group.error}<p class="muted">Couldn't search {group.label}.</p>{/if}
          {#each group.results as hit (hit.id)}
            <SearchResultRow
              {hit}
              id={optionId(hit)}
              active={active?.id === hit.id}
              actionable={actionable(hit)}
              onclick={() => void openHit(hit)}
            />
          {/each}
          {#if !group.results.length && !group.error}<p class="muted">
              No matches in {group.label}.
            </p>{/if}
        </div>
      {/each}
    </div>
    {#if !$controller.loading && !$controller.error && $controller.query.trim().length >= 2 && !groups.length}<p
        class="muted"
      >
        No matches.
      </p>{/if}
    {#if $controller.selectedSource}<LoadMore
        onload={() => controller.showMore()}
        hasMore={groups.some((group) => group.has_more)}
        loading={$controller.loading}
        label="Show more"
      />{/if}
    {#if !$controller.selectedSource && $controller.query.trim().length >= 2 && $controller.onDemand.length}
      <div class="hstack wrap">
        {#each $controller.onDemand as source}<Button
            variant="secondary"
            onclick={() => void selectSource(source.source)}>Search {source.label}</Button
          >{/each}
      </div>
    {/if}
  </div>
  {#snippet footer()}<span class="caption">↑ ↓ to move · Enter to open · Esc to close</span
    >{/snippet}
</Modal>

<style>
  .search-input-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }
  .search-results {
    max-height: 60dvh;
    overflow: auto;
  }
  .search-group + .search-group {
    margin-top: var(--space-4);
  }
  .recent-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    border: 0;
    border-radius: var(--radius-sm);
    padding: var(--space-2);
    color: var(--text-primary);
    background: transparent;
    text-align: left;
    font: inherit;
    cursor: pointer;
  }
  .recent-row :global(.ui-icon) {
    color: var(--text-muted);
  }
  .recent-row.active,
  .recent-row:hover {
    background: var(--hover-bg);
  }
  h2,
  p {
    margin: 0 0 var(--space-2);
  }
  @media (max-width: 640px) {
    .search-results {
      max-height: none;
    }
  }
</style>
