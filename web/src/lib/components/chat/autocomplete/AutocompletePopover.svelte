<script lang="ts">
  import type { Suggestion } from './types';

  let {
    suggestions,
    activeIndex,
    listId,
    optionId,
    onaccept,
    onhover,
  }: {
    suggestions: Suggestion[];
    activeIndex: number;
    /** id for the listbox (textarea's aria-controls). */
    listId: string;
    /** Maps a suggestion key → its option element id (for aria-activedescendant). */
    optionId: (key: string) => string;
    onaccept: (index: number) => void;
    onhover: (index: number) => void;
  } = $props();

  let listEl: HTMLUListElement | undefined = $state();

  // Keep the highlighted row in view as the selection moves.
  $effect(() => {
    const _ = activeIndex; // track
    if (!listEl) return;
    const row = listEl.querySelector<HTMLElement>('[data-highlighted="true"]');
    // scrollIntoView is absent under jsdom; guard so tests + SSR don't throw.
    if (row && typeof row.scrollIntoView === 'function') {
      row.scrollIntoView({ block: 'nearest' });
    }
  });
</script>

<ul bind:this={listEl} class="ac-list" id={listId} role="listbox" aria-label="Suggestions">
  {#each suggestions as s, i (s.key)}
    <li
      id={optionId(s.key)}
      class="ac-item"
      role="option"
      aria-selected={i === activeIndex}
      data-highlighted={i === activeIndex}
      onmousedown={(e) => {
        e.preventDefault(); // keep textarea focus
        onaccept(i);
      }}
      onmouseenter={() => onhover(i)}
    >
      <span class="ac-label">{s.label}</span>
      {#if s.description}<span class="ac-desc">{s.description}</span>{/if}
    </li>
  {/each}
</ul>

<style>
  .ac-list {
    position: absolute;
    bottom: 100%;
    left: 0;
    margin: 0 0 0.3rem 0;
    padding: 0.25rem;
    list-style: none;
    width: min(28rem, 100%);
    max-height: 15rem;
    overflow-y: auto;
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: 0.4rem;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    z-index: 100;
  }
  .ac-item {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.3rem 0.5rem;
    border-radius: 0.3rem;
    cursor: pointer;
    white-space: nowrap;
  }
  .ac-item[data-highlighted='true'] {
    background: var(--surface-raised);
  }
  .ac-label {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: var(--text-sm);
    color: var(--text-primary);
    flex-shrink: 0;
  }
  .ac-desc {
    font-size: var(--text-xs);
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
