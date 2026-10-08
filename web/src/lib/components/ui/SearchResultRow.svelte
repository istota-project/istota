<script lang="ts">
  import type { SearchHit } from '$lib/api';
  import { segments } from '$lib/search/highlight';
  import { formatRelative } from '$lib/dateFormat';
  import Badge from './Badge.svelte';
  let {
    hit,
    id,
    active,
    actionable,
    onclick,
  }: {
    hit: SearchHit;
    id: string;
    active: boolean;
    actionable: boolean;
    onclick: () => void;
  } = $props();
</script>

<button
  {id}
  type="button"
  role="option"
  aria-selected={active}
  disabled={!actionable}
  tabindex="-1"
  class="search-result"
  class:active
  {onclick}
>
  <span class="result-heading"
    ><strong>{hit.title}</strong>{#if hit.date}<time class="caption" datetime={hit.date}
        >{formatRelative(hit.date)}</time
      >{/if}</span
  >
  {#if hit.subtitle}<span class="caption">{hit.subtitle}</span>{/if}
  <span class="result-snippet"
    >{#each segments(hit.snippet, hit.highlights) as segment}{#if segment.mark}<mark
          >{segment.text}</mark
        >{:else}{segment.text}{/if}{/each}</span
  >
  {#if hit.badges.length}<span class="result-badges"
      >{#each hit.badges as badge}<Badge>{badge}</Badge>{/each}</span
    >{/if}
</button>

<style>
  .search-result {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    width: 100%;
    border: 0;
    border-radius: var(--radius-sm);
    padding: var(--space-2);
    color: var(--text-primary);
    background: transparent;
    text-align: left;
    font: inherit;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .search-result.active,
  .search-result:hover:not(:disabled) {
    background: var(--surface-raised);
  }
  .search-result:disabled {
    cursor: default;
  }
  .result-heading {
    display: flex;
    justify-content: space-between;
    gap: var(--space-2);
    width: 100%;
  }
  .result-heading strong {
    min-width: 0;
  }
  time {
    flex-shrink: 0;
  }
  .result-snippet {
    color: var(--text-secondary);
  }
  mark {
    color: var(--accent-amber-fill-fg);
    background: var(--accent-amber-fill);
  }
  .result-badges {
    display: flex;
    gap: var(--space-1);
  }
</style>
