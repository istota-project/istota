<svelte:options runes={true} />

<script lang="ts">
  import { untrack, type Snippet } from 'svelte';
  import { ChevronRight } from '@lucide/svelte';
  import Icon from './Icon.svelte';
  import { loadSetting, saveSetting } from '$lib/ui/persisted';

  interface Props {
    label: string;
    count?: number;
    collapsible?: boolean;
    defaultOpen?: boolean;
    storageKey?: string;
    /** When provided, the label becomes a filter button (caret still
     *  collapses) and the header can render an active/selected state. */
    onSelect?: () => void;
    active?: boolean;
    /** Bindable, for a caller that has to open the group itself (chat opens
     *  "Email threads" when one of its rooms is on screen). Seeded from
     *  `defaultOpen` when not bound. */
    open?: boolean;
    children: Snippet;
  }

  let {
    label,
    count,
    collapsible = false,
    defaultOpen = true,
    storageKey,
    onSelect,
    active = false,
    open = $bindable(
      untrack(() => {
        const saved = storageKey ? loadSetting<unknown>(storageKey, defaultOpen) : defaultOpen;
        return typeof saved === 'boolean' ? saved : defaultOpen;
      }),
    ),
    children,
  }: Props = $props();

  function toggle(e: MouseEvent) {
    e.stopPropagation();
    open = !open;
    if (storageKey) saveSetting(storageKey, open);
  }
</script>

<div class="cat-group">
  {#if collapsible && onSelect}
    <!-- Selectable header: caret collapses, label filters to this category. -->
    <div class="cat-header" class:active>
      <button
        class="caret-btn touch-target"
        onclick={toggle}
        type="button"
        aria-label="Show {label}"
        aria-expanded={open}
      >
        <span class="caret" class:open><Icon icon={ChevronRight} size="xs" /></span>
      </button>
      <button class="cat-select micro-label" onclick={onSelect} type="button">
        <span class="cat-label-text">{label}</span>
        {#if count !== undefined}<span class="cat-count caption num">{count}</span>{/if}
      </button>
    </div>
    {#if open}
      {@render children()}
    {/if}
  {:else if collapsible}
    <button
      class="cat-label cat-label-button micro-label"
      onclick={toggle}
      type="button"
      aria-expanded={open}
    >
      <span class="caret" class:open><Icon icon={ChevronRight} size="xs" /></span>
      <span class="cat-label-text">{label}</span>
      {#if count !== undefined}<span class="cat-count caption num">{count}</span>{/if}
    </button>
    {#if open}
      {@render children()}
    {/if}
  {:else}
    <div class="cat-label micro-label">
      <span class="cat-label-text">{label}</span>
      {#if count !== undefined}<span class="cat-count caption num">{count}</span>{/if}
    </div>
    {@render children()}
  {/if}
</div>

<style>
  .cat-group {
    margin-bottom: var(--space-1);
  }

  .cat-group > :global(p) {
    margin: var(--space-1) var(--space-3);
  }

  .cat-label {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3) 0.15rem;
  }

  .cat-label-button {
    width: 100%;
    background: none;
    border: none;
    /* Family and leading only: the shorthand would reset the size and weight
       .micro-label gives the button. */
    font-family: inherit;
    line-height: inherit;
    cursor: pointer;
    text-align: left;
    transition: color var(--transition-fast);
  }

  .cat-label-button:hover {
    color: var(--text-muted);
  }

  /* Selectable header (onSelect provided): caret + filter-label in one row. */
  .cat-header {
    display: flex;
    align-items: stretch;
    gap: 0.1rem;
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .cat-header:hover {
    background: var(--hover-bg);
  }

  .cat-header.active {
    background: var(--selected-bg);
  }

  .caret-btn {
    display: inline-flex;
    align-items: center;
    background: none;
    border: none;
    cursor: pointer;
    padding: var(--space-2) 0.15rem 0.15rem var(--space-2);
  }

  .cat-select {
    flex: 1;
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    background: none;
    border: none;
    font-family: inherit;
    line-height: inherit;
    cursor: pointer;
    text-align: left;
    padding: var(--space-2) var(--space-3) 0.15rem 0;
    transition: color var(--transition-fast);
  }

  .cat-header:hover .caret,
  .cat-header:hover .cat-select {
    color: var(--text-muted);
  }

  .cat-header.active .cat-select,
  .cat-header.active .caret {
    color: var(--text-primary);
  }

  /* Disclosure's marker: a ChevronRight at --icon-xs, turned down when open. */
  .caret {
    display: inline-flex;
    flex: none;
    align-self: center;
    color: var(--text-dim);
    transition: transform var(--transition-fast);
  }

  .caret.open {
    transform: rotate(90deg);
  }

  .cat-count {
    text-transform: none;
    letter-spacing: 0;
  }
  @media (pointer: coarse) {
    .caret-btn {
      min-width: var(--touch-min);
      --touch-target-w: 100%;
    }

    .cat-header {
      gap: var(--space-1);
    }
  }
</style>
