<script module lang="ts">
  import type { SortDirection } from '$lib/ui/tableSort.svelte';

  /** The `aria-sort` value for the `th` holding a SortHeader. */
  export function sortAria(direction: SortDirection | null): SortDirection | 'none' {
    return direction ?? 'none';
  }
</script>

<script lang="ts">
  import { ArrowDown, ArrowUp, ArrowUpDown } from '@lucide/svelte';

  interface Props {
    label: string;
    /** Lowercase name read by a screen reader ("invoice number"); defaults to the label. */
    name?: string;
    /** `null` when this column is not the sort. */
    direction: SortDirection | null;
    onclick: () => void;
    /** Placement only: the page's column class. */
    class?: string;
  }

  let { label, name, direction, onclick, class: column = '' }: Props = $props();

  const spoken = $derived(name ?? label.toLowerCase());
  const description = $derived(
    direction ? `Sorted by ${spoken}, ${direction}. Reverse the order` : `Sort by ${spoken}`,
  );
</script>

<!--
  The button only: a column label that sorts. It takes its type from the
  header it sits in (a table's `th`, a record list's header row), which is why
  it inherits case and tracking — a button resets both. In a `th`, put
  `aria-sort={sortAria(direction)}` on the cell; in a div header there is no
  cell, and the spoken label carries the state.
-->
<button
  type="button"
  class="button-reset sort-header {column}"
  class:is-sorted={direction !== null}
  aria-label={description}
  title={description}
  {onclick}
>
  {label}<span class="sort-arrow" aria-hidden="true"
    >{#if !direction}<ArrowUpDown size={12} />{:else if direction === 'descending'}<ArrowDown
        size={12}
      />{:else}<ArrowUp size={12} />{/if}</span
  >
</button>

<style>
  /* Weightless, so a page's placement rule (hiding a column below 640px)
     always wins rather than tying on specificity and stylesheet order. */
  :where(.sort-header) {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    text-align: left;
    text-transform: inherit;
    letter-spacing: inherit;
  }

  .sort-header:hover,
  .sort-header.is-sorted {
    color: var(--text-muted);
  }

  /* An up/down pair at rest, the direction once sorted. Dim until it is the
     sort, so the header reads which column is in charge. */
  .sort-arrow {
    display: inline-flex;
    opacity: var(--disabled-opacity);
  }

  .sort-header:hover .sort-arrow,
  .sort-header.is-sorted .sort-arrow {
    opacity: 1;
  }
</style>
