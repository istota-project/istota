<script lang="ts">
  import { tick, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import type { SearchHit } from '$lib/api';
  import { createSearch } from '$lib/stores/search';
  import { hrefFor } from '$lib/search/links';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import Modal from './Modal.svelte';
  import Input from './Input.svelte';
  import Chip from './Chip.svelte';
  import Button from './Button.svelte';
  import SearchResultRow from './SearchResultRow.svelte';

  let {
    open = $bindable(false),
    controller = createSearch(),
  }: {
    open?: boolean;
    controller?: ReturnType<typeof createSearch>;
  } = $props();
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

  $effect(() => {
    if (!open) controller.close();
  });
  onDestroy(() => controller.close());
  $effect(() => {
    if (open && active)
      document.getElementById(optionId(active))?.scrollIntoView?.({ block: 'nearest' });
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

  function handleKeydown(event: KeyboardEvent) {
    if (event.isComposing) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (options.length)
        controller.setActive(
          ($controller.activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + options.length) %
            options.length,
        );
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

<Modal bind:open title="Search" variant="palette">
  <div class="search-input-row">
    <Input
      id={inputId}
      role="combobox"
      aria-label="Search everything"
      aria-expanded={true}
      aria-controls={listId}
      aria-activedescendant={active ? optionId(active) : undefined}
      aria-autocomplete="list"
      autocomplete="off"
      maxlength={200}
      placeholder="Search messages, notes, and more…"
      value={$controller.query}
      oninput={(event) => controller.setQuery(event.currentTarget.value)}
      onkeydown={handleKeydown}
    />
    <Button
      variant="ghost"
      onclick={() => {
        open = false;
      }}>Close</Button
    >
  </div>
  <div class="search-chips" aria-label="Search sources">
    <Chip
      checked={!$controller.selectedSource}
      aria-pressed={!$controller.selectedSource}
      onclick={() => void selectSource(null)}>All</Chip
    >
    {#each $controller.sources as source}
      <Chip
        checked={$controller.selectedSource === source.source}
        aria-pressed={$controller.selectedSource === source.source}
        onclick={() => void selectSource(source.source)}>{source.label}</Chip
      >
    {/each}
  </div>
  <div class="search-results">
    {#if !$controller.query.trim()}
      {#if $controller.recentQueries.length}
        <p class="micro-label">Recent searches</p>
        {#each $controller.recentQueries as query}<Button
            variant="ghost"
            onclick={() => {
              controller.setQuery(query);
              document.getElementById(inputId)?.focus();
            }}>{query}</Button
          >{/each}
      {:else}<p class="muted">Type at least 2 characters.</p>{/if}
    {:else if $controller.query.trim().length < 2}<p class="muted">
        Type at least 2 characters.
      </p>{/if}
    {#if $controller.loading}<p role="status" class="muted">Searching…</p>{/if}
    {#if $controller.error}<p role="status" class="muted">{$controller.error}</p>{/if}
    <div id={listId} role="listbox" aria-label="Search results" aria-busy={$controller.loading}>
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
    {#if $controller.selectedSource && groups.some((group) => group.has_more)}<Button
        variant="ghost"
        disabled={$controller.loading}
        onclick={() => controller.showMore()}>Show more</Button
      >{/if}
    {#if !$controller.selectedSource && $controller.query.trim().length >= 2}
      {#each $controller.onDemand as source}<Button
          variant="ghost"
          onclick={() => void selectSource(source.source)}>Search {source.label}</Button
        >{/each}
    {/if}
  </div>
  {#snippet footer()}<span class="caption">↑ ↓ to move · Enter to open · Esc to close</span
    >{/snippet}
</Modal>

<style>
  .search-input-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .search-chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    margin-block: var(--space-3);
  }
  .search-results {
    max-height: 60dvh;
    overflow: auto;
  }
  .search-group + .search-group {
    margin-top: var(--space-4);
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
